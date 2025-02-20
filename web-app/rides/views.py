from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.mail import send_mail
from django.db import IntegrityError
from django.db.models import F, Q, Sum, Value
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse_lazy
from datetime import datetime


from .models import Ride, RideShare
from django.shortcuts import render
from django.views.generic import ListView, CreateView, UpdateView, DetailView, DeleteView
from .forms import RideRequestForm,RideShareForm
from django.contrib.auth.mixins import LoginRequiredMixin
from .mixins import DriverRequiredMixin, OwnerRequiredMixin

from django.db.models.functions import Coalesce
# Create your views here.
class OpenRideListView(ListView):
    model = Ride
    template_name = 'rides/ride_list.html'
    context_object_name = 'rides_list'
    paginate_by = 5
    ordering = ['-created_at']

    def get_queryset(self):
        qs = super().get_queryset().filter(
            Q(status=Ride.Status.OPEN)
        )
        # 使用 annotate 计算每个 Ride 的总乘客数：
        # 总乘客数 = owner_passengers + (所有 ride_share 中的 passenger 数量之和)
        # 使用 Coalesce 处理没有 ride_share 时 Sum 返回 None 的情况
        # ride_share__passenger表示访问 Ride 对象相关联的所有 RideShare 对象中的 passenger 字段
        # F函数：动态地获取值
        qs = qs.annotate(
            total_people=F('owner_passengers') + Coalesce(Sum('ride_share__passenger'), Value(0))
        )


        destination = self.request.GET.get('destination', '').strip()
        if destination:
            qs = qs.filter(destination__icontains=destination)

        # 到达时间区间搜索
        arrival_time_start = self.request.GET.get('arrival_time_start', '').strip()
        arrival_time_end = self.request.GET.get('arrival_time_end', '').strip()

        #  'YYYY-MM-DDTHH:MM'
        if arrival_time_start:
            try:
                start_datetime = datetime.strptime(arrival_time_start, '%Y-%m-%dT%H:%M')
                qs = qs.filter(scheduled_datetime__gte=start_datetime)
            except ValueError:
                pass

        if arrival_time_end:
            try:
                end_datetime = datetime.strptime(arrival_time_end, '%Y-%m-%dT%H:%M')
                qs = qs.filter(scheduled_datetime__lte=end_datetime)
            except ValueError:
                pass

        special_request = self.request.GET.get('special_request', '').strip()
        if special_request:
            qs = qs.filter(special_request__exact=special_request)

        cap_check = self.request.GET.get('cap_check', '').strip()
        if cap_check:

            qs = qs.filter(total_people__lte=cap_check)

        # 已经搭乘的乘客数量搜索
        passengers = self.request.GET.get('passengers', '').strip()
        if passengers:
            try:
                passengers = int(passengers)
                qs = qs.filter(total_people__exact=passengers)
            except ValueError:
                pass


        if self.request.user.is_authenticated:
            current_profile = self.request.user.userprofile
            # exclude the ride which user already join
            qs = qs.exclude(ride_share__sharer=current_profile)

            if not self.request.user.userprofile.is_driver:
                qs = qs.exclude(owner=current_profile).filter(can_shared=True)

        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        context['destination'] = self.request.GET.get('destination', '')
        context['arrival_time_start'] = self.request.GET.get('arrival_time_start', '')
        context['arrival_time_end'] = self.request.GET.get('arrival_time_end', '')
        context['passengers'] = self.request.GET.get('passengers', '')
        context['special_request'] = self.request.GET.get('special_request', '')
        context['cap_check'] = self.request.GET.get('cap_check', '')

        querydict = self.request.GET.copy()
        if 'page' in querydict:
            querydict.pop('page')
        context['query_string'] = querydict.urlencode()
        return context



class MyRidesView(ListView):
    model = Ride
    template_name = 'rides/my_ride_list.html'
    context_object_name = 'rides_list'
    paginate_by = 5
    ordering = ['-scheduled_datetime']

    def get_queryset(self):
        user = self.request.user
        user_profile = user.userprofile

        my_confirmed = self.request.GET.get("my_confirmed")
        if my_confirmed == 'on':
            if hasattr(user, 'driverprofile'):
                return Ride.objects.filter(driver=user.driverprofile, status="CONFIRMED")
            else:
                return Ride.objects.none()

        # 获取 GET 参数中的 status，如果有则使用该状态过滤
        status_query = self.request.GET.get('status')

        if status_query:
            owned = Ride.objects.filter(owner=user_profile, status=status_query)
            sharer_rides = RideShare.objects.filter(sharer=user_profile).select_related('ride')
            sharer = Ride.objects.filter(id__in=sharer_rides.values('ride_id'), status=status_query)
            driven = Ride.objects.none()
            if hasattr(user, 'driverprofile'):
                driven = Ride.objects.filter(driver=user.driverprofile, status=status_query)
        else:
        # 默认：排除 COMPLETED 状态
            owned = Ride.objects.filter(owner=user_profile).exclude(status=Ride.Status.COMPLETED)
            sharer_rides = RideShare.objects.filter(sharer=user_profile).select_related('ride')
            sharer = Ride.objects.filter(id__in=sharer_rides.values('ride_id')).exclude(status=Ride.Status.COMPLETED)
            driven = Ride.objects.none()
            if hasattr(user, 'driverprofile'):
                driven = Ride.objects.filter(driver=user.driverprofile).exclude(status=Ride.Status.COMPLETED)

        return owned.union(sharer, driven)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # 将 Ride 模型的状态选项传递给模板
        context['ride_status_choices'] = Ride.Status.choices
        querydict = self.request.GET.copy()
        if 'page' in querydict:
            querydict.pop('page')
        context['query_string'] = querydict.urlencode()
        return context


class RideDetailView(DetailView):
    model = Ride
    template_name = 'rides/ride_detail.html'
    context_object_name = 'ride'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        ride = self.object

        if user.is_authenticated:
            if user.userprofile.is_driver:
                # see if the one who claim the ride is the exact driver of this ride
                context['driver_claimed'] = ride.driver == user.driverprofile
                # check if ride sharer list contains the driver(user)
                context['driver_join'] = ride.ride_share.filter(sharer = user.userprofile).exists()
            else:
                context['has_joined'] = ride.ride_share.filter(sharer=user.userprofile).exists()
        else:
            context['has_joined'] = False

        return context





# owner create ride
class RideCreateView(LoginRequiredMixin, CreateView):
    model = Ride
    form_class = RideRequestForm
    template_name = 'rides/ride_create.html'
    success_url = reverse_lazy('rides:my-ride-list')


    # logic for form created successfully
    def form_valid(self, form):
        form.instance.owner = self.request.user.userprofile

        form.instance.status = Ride.Status.OPEN
        return super().form_valid(form)


class RideCancelView(LoginRequiredMixin, OwnerRequiredMixin, UpdateView):
    model = Ride
    template_name = 'rides/ride_cancel.html'
    success_url = reverse_lazy('rides:my-ride-list')
    fields = []
    def form_valid(self, form):
        if self.object.status == Ride.Status.OPEN:
            self.object = self.get_object()
            self.object.status = Ride.Status.CANCELLED
            self.object.save()
            messages.success(self.request, f"Ride #{self.object.id} has been cancelled")
        else:
            messages.error(self.request, f"Ride #{self.object.id} cannot be cancelled")
        return redirect('rides:my-ride-list')


class RideEditView(LoginRequiredMixin, OwnerRequiredMixin, UpdateView):
    model = Ride
    form_class = RideRequestForm
    template_name = 'rides/ride_edit.html'
    success_url = reverse_lazy('rides:my-ride-list')
    def form_valid(self, form):
        if form.instance.status != Ride.Status.OPEN:
            messages.error(self.request, "Cannot edit this form")
            return redirect('rides:ride-list')
        return super().form_valid(form)


class RideQuitView(LoginRequiredMixin, DeleteView):
    model = RideShare
    template_name = 'rides/ride_quit.html'
    success_url = reverse_lazy('rides:my-ride-list')


    def get_object(self, queryset=None):
        ride_id = self.kwargs.get('pk')

        return get_object_or_404(RideShare, ride_id=ride_id, sharer=self.request.user.userprofile)


    def delete(self, request, *args, **kwargs):
        self.object = self.get_object()
        messages.success(request, f"Ride #{self.object.id} has been quit")
        return super().delete(request, *args, **kwargs)




@login_required()
def ride_join(request, pk):
    ride = get_object_or_404(Ride, pk=pk, status='OPEN', can_shared=True)
    user = request.user
    # reinforce logic
    if ride.owner == user.userprofile:
        messages.error(request, 'You cannot join your own ride')
        return redirect('rides:ride-list')
    if user.userprofile.is_driver and ride.driver == user.driverprofile:
        messages.error(request, 'You already claimed this ride')
        return redirect('rides:ride-list')
    if ride.ride_share.filter(sharer=user.userprofile).exists():
        messages.error(request, 'You have already joined this ride')
        return redirect('rides:ride-list')

    if request.method == 'POST':
        form = RideShareForm(request.POST)
        if form.is_valid():
            ride_share = form.save(commit=False)
            ride_share.ride = ride
            ride_share.sharer = request.user.userprofile
            try:
                ride_share.save()
                messages.success(request, 'You have joined the ride successfully')
            except IntegrityError:
                messages.error(request, 'You have already joined the ride')
            return redirect('rides:my-ride-list')
    else:
        form = RideShareForm()
    return render(request, 'rides/ride_join.html', {'form': form, 'ride': ride})



def send_email_for_ride(ride):
    subject = f"Your ride {ride.id} has been claimed"
    email_messages = f'''
        Dear ride share platform user, your ride to {ride.destination} has been claimed by a driver!
        Ride detail: 
            - Scheduled time: {ride.scheduled_datetime}
            - Destination: {ride.destination}
            - Driver: {ride.driver.driver.username}
    '''
    recipients = [ride.owner.user.email]
    for ride_share in ride.ride_share.all():
        recipients.append(ride_share.sharer.user.email)
    print("Email recipients:", recipients)
    send_mail(subject, email_messages, settings.DEFAULT_FROM_EMAIL, recipients)


@login_required()
def driver_claim_ride(request, pk):
    driver_profile = getattr(request.user, 'driverprofile', None)
    if driver_profile is None:
        messages.error(request, 'You are not a driver')
        return redirect('rides:ride-list')
    ride = get_object_or_404(Ride, pk=pk, status='OPEN')
    # double check
    if ride.total_amount_people() > driver_profile.maxPassengers:
        messages.error(request, 'Passengers size exceed total capacity')
        return redirect('rides:ride-list')
    if ride.vehicle_type_request and ride.vehicle_type_request != driver_profile.vehicleType:
        messages.error(request, 'Does not satisfy vehicle type request')
        return redirect('rides:ride-list')
    if ride.special_request and ride.special_request != driver_profile.special_info:
        print(f"ride.special_request:{ride.special_request}")
        print(f"driver_profile.special_info:{driver_profile.special_info}")
        messages.error(request, 'Does not satisfy special request')
        return redirect('rides:ride-list')
    ride.driver = request.user.driverprofile
    ride.status = 'CONFIRMED'
    ride.save()


    send_email_for_ride(ride)

    if ride.driver and hasattr(ride.driver, 'driver') and ride.driver.driver.email:
        driver_subject = f"You have claimed ride #{ride.id}"
        driver_message = (
            f"Dear {ride.driver.driver.username},\n\n"
            f"You have successfully claimed ride #{ride.id}.\n"
            f"Ride Details:\n"
            f" - Destination: {ride.destination}\n"
            f" - Scheduled Time: {ride.scheduled_datetime}\n\n"
            "Please contact the ride owner for further details.\n\n"
            f"Ride Owner Contact Information:\n"
            f" - Name: {ride.owner.name}\n"
            f" - Email: {ride.owner.user.email}\n"
            f" - Email: {ride.owner.phone}\n"
            "Your Ride Sharing Team"
        )
        send_mail(driver_subject, driver_message, settings.DEFAULT_FROM_EMAIL, [ride.driver.driver.email])
    messages.success(request, f"You have successfully claimed ride #{ride.id}!")
    return redirect('rides:ride-list')


@login_required()
def driver_complete_ride(request, pk):
    ride = get_object_or_404(Ride, pk=pk, status='CONFIRMED', driver = request.user.driverprofile)
    if request.method == 'POST':
        ride.status = 'COMPLETED'
        ride.save()
        messages.success(request, f"You have successfully completed ride #{ride.id}!")
        return redirect('rides:ride-list')
    return render(request, 'rides/driver_complete.html', {'ride': ride})





