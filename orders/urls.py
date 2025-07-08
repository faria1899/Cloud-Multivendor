from django.urls import path
from . import views

urlpatterns = [
    path('place_order/', views.place_order, name='place_order'),
    path('invoice/<order_number>/', views.invoice_view, name='invoice'),
    #path('order_complete/', views.order_complete, name='order_complete'),
    path('sslcommerz_success/', views.sslcommerz_success, name='sslcommerz_success'),
    path('sslcommerz_fail/', views.sslcommerz_fail, name='sslcommerz_fail'),
    path('sslcommerz_cancel/',views.sslcommerz_cancel, name='sslcommerz_cancel'),
    path('order_complete/', views.order_complete, name='order_complete'),
]