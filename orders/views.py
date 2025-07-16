import json
from decimal import Decimal
from django.http import JsonResponse, HttpResponse
from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.contrib.sites.shortcuts import get_current_site
from django.views.decorators.csrf import csrf_exempt
from django.urls import reverse
from marketplace.models import Cart, Tax
from marketplace.context_processors import get_cart_amounts
from menu.models import FoodItem
from .forms import OrderForm
from .models import Order, OrderedFood, Payment
from .utils import generate_order_number, order_total_by_vendor
from accounts.utils import send_notification
import json
from django.shortcuts import render, redirect
from .models import Order, OrderedFood, Payment
from django.shortcuts import render, get_object_or_404

from sslcommerz_python_api import SSLCSession
from django.conf import settings

# Helper to convert Decimal to str recursively

def convert_decimal_to_str(obj):
    if isinstance(obj, dict):
        return {k: convert_decimal_to_str(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [convert_decimal_to_str(i) for i in obj]
    elif isinstance(obj, Decimal):
        return str(obj)
    return obj


@login_required(login_url='login')
def place_order(request):
    cart_items = Cart.objects.filter(user=request.user).order_by('created_at')
    if not cart_items.exists():
        return redirect('marketplace')

    get_tax = Tax.objects.filter(is_active=True)
    subtotal_map = {}
    total_data = {}
    vendors_ids = []

    for item in cart_items:
        vendor_id = item.fooditem.vendor.id
        vendors_ids.append(vendor_id)
        vendors_ids = list(set(vendors_ids))

        price = item.fooditem.price * item.quantity
        subtotal_map[vendor_id] = subtotal_map.get(vendor_id, 0) + price

        tax_dict = {}
        for tax in get_tax:
            tax_amount = round((tax.tax_percentage * subtotal_map[vendor_id]) / 100, 2)
            tax_dict[tax.tax_type] = {str(tax.tax_percentage): str(tax_amount)}

        total_data[vendor_id] = {str(subtotal_map[vendor_id]): str(tax_dict)}

    cart_totals = get_cart_amounts(request)

    if request.method == 'POST':
        if request.POST.get('pay_now'):  # Step 2: Trigger payment gateway
            order_number = request.POST.get('order_number')
            try:
                order = Order.objects.get(order_number=order_number, is_ordered=False)

                payment_session = SSLCSession(
                    sslc_is_sandbox=True,
                    sslc_store_id=settings.SSLC_STORE_ID,
                    sslc_store_pass=settings.SSLC_STORE_PASS
                )

                current_site = get_current_site(request)
                domain = f"http://{current_site}"

                payment_session.set_urls(
                    success_url=domain + "/orders/sslcommerz_success/",
                    fail_url=domain + "/orders/sslcommerz_fail/",
                    cancel_url=domain + "/orders/sslcommerz_cancel/",
                )

                payment_session.set_product_integration(
                    total_amount=Decimal(order.total),
                    currency='BDT',
                    product_category='Food',
                    product_name='FoodOrder',
                    num_of_item=len(cart_items),
                    shipping_method='NO',
                    product_profile='general'
                )

                payment_session.set_customer_info(
                    name=order.name,
                    email=order.email,
                    address1=order.address,
                    address2=order.address,
                    city=order.city,
                    postcode=order.pin_code,
                    country=order.country,
                    phone=order.phone
                )
                payment_session.set_additional_values(value_a=order.order_number)

                response_data = payment_session.init_payment()
                if response_data.get('status') == 'SUCCESS' and 'GatewayPageURL' in response_data:
                    return redirect(response_data['GatewayPageURL'])
                else:
                    error_msg = response_data.get('failedreason', 'Unknown error during payment gateway initialization.')
                    return HttpResponse(f"Payment gateway error: {error_msg}", status=500)

            except Order.DoesNotExist:
                return HttpResponse("Order not found.", status=404)

        else:  # Step 1: Save order and show place_order.html
            form = OrderForm(request.POST)
            if form.is_valid():
                order = form.save(commit=False)
                order.user = request.user
                order.total = cart_totals['grand_total']
                order.tax_data = json.dumps(convert_decimal_to_str(cart_totals['tax_dict']))
                order.total_data = json.dumps(convert_decimal_to_str(total_data))
                order.total_tax = cart_totals['tax']
                order.payment_method = request.POST['payment_method']
                order.save()
                order.order_number = generate_order_number(order.id)
                order.vendors.add(*vendors_ids)
                order.save()

                return render(request, 'orders/place_order.html', {
                    'order': order,
                    'cart_items': cart_items,
                    'subtotal': cart_totals['subtotal'],
                    'tax_dict': cart_totals['tax_dict'],
                    'grand_total': cart_totals['grand_total']
                })
            else:
                print(form.errors)

    return redirect('checkout')

@csrf_exempt
def sslcommerz_success(request):
    if request.method == 'POST':
        data = request.POST
        order_number = data.get('value_a')
        transaction_id = data.get('tran_id')
        payment_method = data.get('card_issuer', 'SSLCommerz')
        status = data.get('status')

        try:
            order = Order.objects.get(order_number=order_number, is_ordered=False)

            # Create Payment
            payment = Payment.objects.create(
                user=order.user,
                transaction_id=transaction_id,
                payment_method=payment_method,
                amount=order.total,
                status=status
            )

            # Update order
            order.payment = payment
            order.is_ordered = True
            order.save()

            # Move cart items to OrderedFood
            cart_items = Cart.objects.filter(user=order.user)
            for item in cart_items:
                OrderedFood.objects.create(
                    order=order,
                    payment=payment,
                    user=order.user,
                    fooditem=item.fooditem,
                    quantity=item.quantity,
                    price=item.fooditem.price,
                    amount=item.fooditem.price * item.quantity,
                )

            # Send confirmation email to customer
            try:
                mail_subject = 'Thank you for ordering with us.'
                mail_template = 'orders/order_confirmation_email.html'

                ordered_food = OrderedFood.objects.filter(order=order)
                customer_subtotal = sum(item.price * item.quantity for item in ordered_food)
                tax_data = json.loads(order.tax_data)

                context = {
                    'user': order.user,
                    'order': order,
                    'to_email': order.email,
                    'ordered_food': ordered_food,
                    'domain': get_current_site(request),
                    'customer_subtotal': customer_subtotal,
                    'tax_data': tax_data,
                }

                send_notification(mail_subject, mail_template, context)
            except Exception as e:
                print("❌ Customer email failed:", e)

            # Send email to vendors
            try:
                mail_subject = 'You have received a new order.'
                mail_template = 'orders/new_order_received.html'
                to_emails = []

                for item in cart_items:
                    vendor_email = item.fooditem.vendor.user.email
                    if vendor_email not in to_emails:
                        to_emails.append(vendor_email)

                        ordered_food_to_vendor = OrderedFood.objects.filter(
                            order=order, fooditem__vendor=item.fooditem.vendor)

                        vendor_totals = order_total_by_vendor(order, item.fooditem.vendor.id)

                        context = {
                            'order': order,
                            'to_email': vendor_email,
                            'ordered_food_to_vendor': ordered_food_to_vendor,
                            'vendor_subtotal': vendor_totals['subtotal'],
                            'tax_data': vendor_totals['tax_dict'],
                            'vendor_grand_total': vendor_totals['grand_total'],
                        }

                        send_notification(mail_subject, mail_template, context)
            except Exception as e:
                print("❌ Vendor email failed:", e)

            # Clear cart
            cart_items.delete()

            # Redirect to order_complete page
            return redirect(
                reverse('order_complete') + f'?order_no={order_number}&trans_id={transaction_id}'
            )

        except Order.DoesNotExist:
            return HttpResponse("❌ Order does not exist.", status=404)

    return HttpResponse("❌ Invalid request.", status=400)


@csrf_exempt
def sslcommerz_fail(request):
    return HttpResponse("Payment Failed. Please try again.")


@csrf_exempt
def sslcommerz_cancel(request):
    return HttpResponse("Payment Cancelled.")


def order_complete(request):
    order_no = request.GET.get('order_no')
    trans_id = request.GET.get('trans_id')

    try:
        order = Order.objects.get(order_number=order_no, is_ordered=True)
        payment = Payment.objects.get(transaction_id=trans_id)
        ordered_items = OrderedFood.objects.filter(order=order)

        # Load tax_data from JSON string
        tax_data = json.loads(order.tax_data)

        # Calculate subtotal
        subtotal = sum(item.price * item.quantity for item in ordered_items)

        context = {
            'order': order,
            'payment': payment,
            'ordered_items': ordered_items,
            'tax_data': tax_data,
            'subtotal': subtotal,
        }

        return render(request, 'orders/order_complete.html', context)

    except (Order.DoesNotExist, Payment.DoesNotExist):
        return redirect('home')
    
@login_required
def invoice_view(request, order_number):
    order = get_object_or_404(Order, order_number=order_number, user=request.user)

    ordered_items = OrderedFood.objects.filter(order=order)
    
    subtotal = 0
    for item in ordered_items:
        subtotal += item.fooditem.price * item.quantity

    # Parse tax_data from JSON string if stored as text
    import json
    try:
        tax_data = json.loads(order.tax_data)
    except Exception:
        tax_data = {}

    context = {
        'order': order,
        'ordered_items': ordered_items,
        'subtotal': subtotal,
        'tax_data': tax_data,
    }
    return render(request, 'orders/invoice.html', context)