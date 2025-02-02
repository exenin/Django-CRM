import json
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.sites.shortcuts import get_current_site
from django.core.mail import EmailMessage
from django.db.models import Q
from django.forms.models import modelformset_factory
from django.http import HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.views.generic import (
    CreateView, DetailView, ListView, TemplateView, View)

from accounts.models import Account, Tags
from common.models import User, Comment, Attachments, APISettings
from common.utils import LEAD_STATUS, LEAD_SOURCE, COUNTRIES
from common import status
from contacts.models import Contact
from leads.models import Lead
from leads.forms import LeadCommentForm, LeadForm, LeadAttachmentForm
from planner.models import Event, Reminder
from planner.forms import ReminderForm
from leads.tasks import send_lead_assigned_emails
from django.core.exceptions import PermissionDenied
from common.tasks import send_email_user_mentions
from leads.tasks import send_email_to_assigned_user
from common.access_decorators_mixins import (
    sales_access_required, marketing_access_required, SalesAccessRequiredMixin, MarketingAccessRequiredMixin)
from teams.models import Teams


class LeadListView(SalesAccessRequiredMixin, LoginRequiredMixin, TemplateView):
    model = Lead
    context_object_name = "lead_obj"
    template_name = "leads.html"

    def get_queryset(self):
        queryset = self.model.objects.all().exclude(status='converted')
        if (self.request.user.role != "ADMIN" and not
                self.request.user.is_superuser):
            queryset = queryset.filter(
                Q(assigned_to__in=[self.request.user]) |
                Q(created_by=self.request.user))

        request_post = self.request.POST
        if request_post:
            if request_post.get('name'):
                queryset = queryset.filter(
                    Q(first_name__icontains=request_post.get('name')) &
                    Q(last_name__icontains=request_post.get('name')))
            if request_post.get('city'):
                queryset = queryset.filter(
                    city__icontains=request_post.get('city'))
            if request_post.get('email'):
                queryset = queryset.filter(
                    email__icontains=request_post.get('email'))
            if request_post.get('status'):
                queryset = queryset.filter(status=request_post.get('status'))
            if request_post.get('tag'):
                queryset = queryset.filter(tags__in=request_post.getlist('tag'))
            if request_post.get('source'):
                queryset = queryset.filter(source=request_post.get('source'))
            if request_post.getlist('assigned_to'):
                queryset = queryset.filter(
                    assigned_to__id__in=request_post.getlist('assigned_to'))
        return queryset.distinct()

    def get_context_data(self, **kwargs):
        context = super(LeadListView, self).get_context_data(**kwargs)
        # context["lead_obj"] = self.get_queryset()
        open_leads = self.get_queryset().exclude(status='closed')
        close_leads = self.get_queryset().filter(status='closed')
        context["status"] = LEAD_STATUS
        context["open_leads"] = open_leads
        context["close_leads"] = close_leads
        context["per_page"] = self.request.POST.get('per_page')
        context["source"] = LEAD_SOURCE
        context["users"] = User.objects.filter(
            is_active=True).order_by('email')
        context["assignedto_list"] = [
            int(i) for i in self.request.POST.getlist('assigned_to', []) if i]
        context["request_tags"] = self.request.POST.getlist('tag')

        search = True if (
            self.request.POST.get('name') or self.request.POST.get('city') or
            self.request.POST.get('email') or self.request.POST.get('tag') or
            self.request.POST.get('status') or
            self.request.POST.get('source') or
            self.request.POST.get('assigned_to')
        ) else False

        context["search"] = search

        tag_ids = list(set(Lead.objects.values_list('tags', flat=True)))
        context["tags"] = Tags.objects.filter(id__in=tag_ids)

        tab_status = 'Open'
        if self.request.POST.get('tab_status'):
            tab_status = self.request.POST.get('tab_status')
        context['tab_status'] = tab_status

        return context

    def post(self, request, *args, **kwargs):
        context = self.get_context_data(**kwargs)
        return self.render_to_response(context)


@login_required
@sales_access_required
def create_lead(request):
    template_name = "create_lead.html"
    users = []
    if request.user.role == 'ADMIN' or request.user.is_superuser:
        users = User.objects.filter(is_active=True).order_by('email')
    elif request.user.google.all():
        users = []
    else:
        users = User.objects.filter(role='ADMIN').order_by('email')
    form = LeadForm(assigned_to=users)

    if request.POST:
        form = LeadForm(request.POST, request.FILES, assigned_to=users)
        if form.is_valid():
            lead_obj = form.save(commit=False)
            lead_obj.created_by = request.user
            lead_obj.save()
            if request.POST.get('tags', ''):
                tags = request.POST.get("tags")
                splitted_tags = tags.split(",")
                for t in splitted_tags:
                    tag = Tags.objects.filter(name=t)
                    if tag:
                        tag = tag[0]
                    else:
                        tag = Tags.objects.create(name=t)
                    lead_obj.tags.add(tag)
            if request.POST.getlist('assigned_to', []):
                lead_obj.assigned_to.add(*request.POST.getlist('assigned_to'))
                assigned_to_list = request.POST.getlist('assigned_to')
                # current_site = get_current_site(request)
                # recipients = assigned_to_list
                # send_email_to_assigned_user.delay(recipients, lead_obj.id, domain=current_site.domain,
                #     protocol=request.scheme)
                # for assigned_to_user in assigned_to_list:
                #     user = get_object_or_404(User, pk=assigned_to_user)
                #     mail_subject = 'Assigned to lead.'
                #     message = render_to_string(
                #         'assigned_to/leads_assigned.html', {
                #             'user': user,
                #             'domain': current_site.domain,
                #             'protocol': request.scheme,
                #             'lead': lead_obj
                #         })
                #     email = EmailMessage(
                #         mail_subject, message, to=[user.email])
                #     email.content_subtype = "html"
                #     email.send()
            if request.POST.getlist('teams', []):
                user_ids = Teams.objects.filter(id__in=request.POST.getlist('teams')).values_list('users', flat=True)
                assinged_to_users_ids = lead_obj.assigned_to.all().values_list('id', flat=True)
                for user_id in user_ids:
                    if user_id not in assinged_to_users_ids:
                        lead_obj.assigned_to.add(user_id)

            current_site = get_current_site(request)
            recipients = list(lead_obj.assigned_to.all().values_list('id', flat=True))
            send_email_to_assigned_user.delay(recipients, lead_obj.id, domain=current_site.domain,
                protocol=request.scheme)

            if request.FILES.get('lead_attachment'):
                attachment = Attachments()
                attachment.created_by = request.user
                attachment.file_name = request.FILES.get(
                    'lead_attachment').name
                attachment.lead = lead_obj
                attachment.attachment = request.FILES.get('lead_attachment')
                attachment.save()

            if request.POST.get('status') == "converted":
                account_object = Account.objects.create(
                    created_by=request.user, name=lead_obj.account_name,
                    email=lead_obj.email, phone=lead_obj.phone,
                    description=request.POST.get('description'),
                    website=request.POST.get('website'),
                )
                account_object.billing_address_line = lead_obj.address_line
                account_object.billing_street = lead_obj.street
                account_object.billing_city = lead_obj.city
                account_object.billing_state = lead_obj.state
                account_object.billing_postcode = lead_obj.postcode
                account_object.billing_country = lead_obj.country
                for tag in lead_obj.tags.all():
                    account_object.tags.add(tag)

                if request.POST.getlist('assigned_to', []):
                    # account_object.assigned_to.add(*request.POST.getlist('assigned_to'))
                    assigned_to_list = request.POST.getlist('assigned_to')
                    current_site = get_current_site(request)
                    recipients = assigned_to_list
                    send_email_to_assigned_user.delay(recipients, lead_obj.id, domain=current_site.domain,
                        protocol=request.scheme)
                    # for assigned_to_user in assigned_to_list:
                    #     user = get_object_or_404(User, pk=assigned_to_user)
                    #     mail_subject = 'Assigned to account.'
                    #     message = render_to_string(
                    #         'assigned_to/account_assigned.html', {
                    #             'user': user,
                    #             'domain': current_site.domain,
                    #             'protocol': request.scheme,
                    #             'account': account_object
                    #         })
                    #     email = EmailMessage(
                    #         mail_subject, message, to=[user.email])
                    #     email.content_subtype = "html"
                    #     email.send()

                account_object.save()
            success_url = reverse('leads:list')
            if request.POST.get("savenewform"):
                success_url = reverse("leads:add_lead")
            return JsonResponse({'error': False, 'success_url': success_url})
        return JsonResponse({'error': True, 'errors': form.errors})
    context = {}
    context["lead_form"] = form
    context["accounts"] = Account.objects.filter(status="open")
    context["users"] = users
    context["countries"] = COUNTRIES
    context["status"] = LEAD_STATUS
    context["source"] = LEAD_SOURCE
    context["assignedto_list"] = [
        int(i) for i in request.POST.getlist('assigned_to', []) if i]

    return render(request, template_name, context)


class LeadDetailView(SalesAccessRequiredMixin, LoginRequiredMixin, DetailView):
    model = Lead
    context_object_name = "lead_record"
    template_name = "view_leads.html"

    def get_context_data(self, **kwargs):
        context = super(LeadDetailView, self).get_context_data(**kwargs)
        user_assgn_list = [
            assigned_to.id for assigned_to in context['object'].assigned_to.all()]
        if self.request.user == context['object'].created_by:
            user_assgn_list.append(self.request.user.id)
        if (self.request.user.role != "ADMIN" and not
                self.request.user.is_superuser):
            if self.request.user.id not in user_assgn_list:
                raise PermissionDenied
        comments = Comment.objects.filter(
            lead__id=self.object.id).order_by('-id')
        attachments = Attachments.objects.filter(
            lead__id=self.object.id).order_by('-id')
        events = Event.objects.filter(
            Q(created_by=self.request.user) | Q(updated_by=self.request.user)
        ).filter(attendees_leads=context["lead_record"])
        meetings = events.filter(event_type='Meeting').order_by('-id')
        calls = events.filter(event_type='Call').order_by('-id')
        RemindersFormSet = modelformset_factory(
            Reminder, form=ReminderForm, can_delete=True)
        reminder_form_set = RemindersFormSet({
            'form-TOTAL_FORMS': '1',
            'form-INITIAL_FORMS': '0',
            'form-MAX_NUM_FORMS': '10',
        })

        assigned_data = []
        for each in context['lead_record'].assigned_to.all():
            assigned_dict = {}
            assigned_dict['id'] = each.id
            assigned_dict['name'] = each.email
            assigned_data.append(assigned_dict)

        if self.request.user.is_superuser or self.request.user.role == 'ADMIN':
            users_mention = list(User.objects.all().values('username'))
        elif self.request.user != context['object'].created_by:
            users_mention = [{'username': context['object'].created_by.username}]
        else:
            users_mention = list(context['object'].assigned_to.all().values('username'))

        context.update({
            "attachments": attachments, "comments": comments,
            "status": LEAD_STATUS, "countries": COUNTRIES,
            "reminder_form_set": reminder_form_set,
            "meetings": meetings, "calls": calls,
            "users_mention": users_mention,
            "assigned_data": json.dumps(assigned_data)})
        return context


@login_required
@sales_access_required
def update_lead(request, pk):
    lead_record = Lead.objects.filter(pk=pk).first()
    template_name = "create_lead.html"
    users = []
    if request.user.role == 'ADMIN' or request.user.is_superuser:
        users = User.objects.filter(is_active=True).order_by('email')
    elif request.user.google.all():
        users = []
    else:
        users = User.objects.filter(role='ADMIN').order_by('email')
    status = request.GET.get('status', None)
    initial = {}
    if status and status == "converted":
        error = "This field is required."
        lead_record.status = "converted"
        initial.update({
            "status": status, "lead": lead_record.id})
    error = ""
    form = LeadForm(instance=lead_record, initial=initial, assigned_to=users)

    if request.POST:
        form = LeadForm(request.POST, request.FILES,
                        instance=lead_record,
                        initial=initial, assigned_to=users)

        if request.POST.get('status') == "converted":
            form.fields['account_name'].required = True
            form.fields['email'].required = True
        else:
            form.fields['account_name'].required = False
            form.fields['email'].required = False
        if form.is_valid():
            assigned_to_ids = lead_record.assigned_to.all().values_list(
                'id', flat=True)
            lead_obj = form.save(commit=False)
            lead_obj.save()
            lead_obj.tags.clear()
            all_members_list = []
            if request.POST.get('tags', ''):
                tags = request.POST.get("tags")
                splitted_tags = tags.split(",")
                for t in splitted_tags:
                    tag = Tags.objects.filter(name=t)
                    if tag:
                        tag = tag[0]
                    else:
                        tag = Tags.objects.create(name=t)
                    lead_obj.tags.add(tag)
            if request.POST.getlist('assigned_to', []):
                if request.POST.get('status') != "converted":

                    current_site = get_current_site(request)

                    assigned_form_users = form.cleaned_data.get(
                        'assigned_to').values_list('id', flat=True)
                    all_members_list = list(
                        set(list(assigned_form_users)) -
                        set(list(assigned_to_ids)))
                    # current_site = get_current_site(request)
                    # recipients = all_members_list
                    # send_email_to_assigned_user.delay(recipients, lead_obj.id, domain=current_site.domain,
                    #     protocol=request.scheme)
                    # if all_members_list:
                    #     for assigned_to_user in all_members_list:
                    #         user = get_object_or_404(User, pk=assigned_to_user)
                    #         mail_subject = 'Assigned to lead.'
                    #         message = render_to_string(
                    #             'assigned_to/leads_assigned.html', {
                    #                 'user': user,
                    #                 'domain': current_site.domain,
                    #                 'protocol': request.scheme,
                    #                 'lead': lead_obj
                    #             })
                    #         email = EmailMessage(
                    #             mail_subject, message, to=[user.email])
                    #         email.content_subtype = "html"
                    #         email.send()

                lead_obj.assigned_to.clear()
                lead_obj.assigned_to.add(*request.POST.getlist('assigned_to'))
            else:
                lead_obj.assigned_to.clear()

            if request.POST.getlist('teams', []):
                user_ids = Teams.objects.filter(id__in=request.POST.getlist('teams')).values_list('users', flat=True)
                assinged_to_users_ids = lead_obj.assigned_to.all().values_list('id', flat=True)
                for user_id in user_ids:
                    if user_id not in assinged_to_users_ids:
                        lead_obj.assigned_to.add(user_id)

            current_site = get_current_site(request)
            recipients = list(lead_obj.assigned_to.all().values_list('id', flat=True))
            send_email_to_assigned_user.delay(recipients, lead_obj.id, domain=current_site.domain,
                protocol=request.scheme)

            if request.FILES.get('lead_attachment'):
                attachment = Attachments()
                attachment.created_by = request.user
                attachment.file_name = request.FILES.get(
                    'lead_attachment').name
                attachment.lead = lead_obj
                attachment.attachment = request.FILES.get('lead_attachment')
                attachment.save()

            if request.POST.get('status') == "converted":
                account_object = Account.objects.create(
                    created_by=request.user, name=lead_obj.account_name,
                    email=lead_obj.email, phone=lead_obj.phone,
                    description=request.POST.get('description'),
                    website=request.POST.get('website'),
                    lead=lead_obj
                )
                account_object.billing_address_line = lead_obj.address_line
                account_object.billing_street = lead_obj.street
                account_object.billing_city = lead_obj.city
                account_object.billing_state = lead_obj.state
                account_object.billing_postcode = lead_obj.postcode
                account_object.billing_country = lead_obj.country
                for tag in lead_obj.tags.all():
                    account_object.tags.add(tag)
                if request.POST.getlist('assigned_to', []):
                    # account_object.assigned_to.add(*request.POST.getlist('assigned_to'))
                    assigned_to_list = request.POST.getlist('assigned_to')
                    current_site = get_current_site(request)
                    recipients = assigned_to_list
                    send_email_to_assigned_user.delay(recipients, lead_obj.id, domain=current_site.domain,
                        protocol=request.scheme)
                    # current_site = get_current_site(request)
                    # for assigned_to_user in assigned_to_list:
                    #     user = get_object_or_404(User, pk=assigned_to_user)
                    #     mail_subject = 'Assigned to account.'
                    #     message = render_to_string(
                    #         'assigned_to/account_assigned.html', {
                    #             'user': user,
                    #             'domain': current_site.domain,
                    #             'protocol': request.scheme,
                    #             'account': account_object
                    #         })
                    #     email = EmailMessage(
                    #         mail_subject, message, to=[user.email])
                    #     email.content_subtype = "html"
                    #     email.send()

                for comment in lead_obj.leads_comments.all():
                    comment.account = account_object
                    comment.save()
                account_object.save()
            status = request.GET.get('status', None)
            success_url = reverse('leads:list')
            if status:
                success_url = reverse('accounts:list')
            return JsonResponse({'error': False, 'success_url': success_url})
        return JsonResponse({'error': True, 'errors': form.errors})
    context = {}
    context["lead_obj"] = lead_record
    user_assgn_list = [
        assigned_to.id for assigned_to in lead_record.assigned_to.all()]
    if request.user == lead_record.created_by:
        user_assgn_list.append(request.user.id)
    if request.user.role != "ADMIN" and not request.user.is_superuser:
        if request.user.id not in user_assgn_list:
            raise PermissionDenied

    context["lead_form"] = form
    context["accounts"] = Account.objects.filter(status="open")
    context["users"] = users
    context["countries"] = COUNTRIES
    context["status"] = LEAD_STATUS
    context["source"] = LEAD_SOURCE
    context["error"] = error
    context["assignedto_list"] = [
        int(i) for i in request.POST.getlist('assigned_to', []) if i]

    return render(request, template_name, context)


class DeleteLeadView(SalesAccessRequiredMixin, LoginRequiredMixin, View):

    def get(self, request, *args, **kwargs):
        return self.post(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        self.object = get_object_or_404(Lead, id=kwargs.get("pk"))
        if (
            self.request.user.role == "ADMIN" or
            self.request.user.is_superuser or
            self.request.user == self.object.created_by
        ):
            self.object.delete()
            return redirect("leads:list")
        raise PermissionDenied


def convert_lead(request, pk):
    lead_obj = get_object_or_404(Lead, id=pk)
    if lead_obj.account_name and lead_obj.email:
        lead_obj.status = 'converted'
        lead_obj.save()
        account_object = Account.objects.create(
            created_by=request.user, name=lead_obj.account_name,
            email=lead_obj.email, phone=lead_obj.phone,
            description=lead_obj.description,
            website=lead_obj.website,
            billing_address_line=lead_obj.address_line,
            billing_street=lead_obj.street,
            billing_city=lead_obj.city,
            billing_state=lead_obj.state,
            billing_postcode=lead_obj.postcode,
            billing_country=lead_obj.country,
            lead=lead_obj
        )
        contacts_list = lead_obj.contacts.all().values_list('id', flat=True)
        account_object.contacts.add(*contacts_list)
        account_obj = account_object.save()
        current_site = get_current_site(request)
        for assigned_to_user in lead_obj.assigned_to.all().values_list(
                'id', flat=True):
            user = get_object_or_404(User, pk=assigned_to_user)
            mail_subject = 'Assigned to account.'
            message = render_to_string('assigned_to/account_assigned.html', {
                'user': user,
                'domain': current_site.domain,
                'protocol': request.scheme,
                'account': account_object
            })
            email = EmailMessage(mail_subject, message, to=[user.email])
            email.content_subtype = "html"
            email.send()
        return redirect("accounts:list")

    return HttpResponseRedirect(
        reverse('leads:edit_lead', kwargs={
            'pk': lead_obj.id}) + '?status=converted')


class AddCommentView(LoginRequiredMixin, CreateView):
    model = Comment
    form_class = LeadCommentForm
    http_method_names = ["post"]

    def post(self, request, *args, **kwargs):
        self.object = None
        self.lead = get_object_or_404(Lead, id=request.POST.get('leadid'))
        if (
            request.user in self.lead.assigned_to.all() or
            request.user == self.lead.created_by or
            request.user.is_superuser or
            request.user.role == 'ADMIN'
        ):
            form = self.get_form()
            if form.is_valid():
                return self.form_valid(form)

            return self.form_invalid(form)

        data = {'error': "You don't have permission to comment."}
        return JsonResponse(data)

    def form_valid(self, form):
        comment = form.save(commit=False)
        comment.commented_by = self.request.user
        comment.lead = self.lead
        comment.save()
        comment_id = comment.id
        current_site = get_current_site(self.request)
        send_email_user_mentions.delay(comment_id, 'leads', domain=current_site.domain,
            protocol=self.request.scheme)
        return JsonResponse({
            "comment_id": comment.id, "comment": comment.comment,
            "commented_on": comment.commented_on,
            "commented_by": comment.commented_by.email
        })

    def form_invalid(self, form):
        return JsonResponse({"error": form['comment'].errors})


class UpdateCommentView(LoginRequiredMixin, View):
    http_method_names = ["post"]

    def post(self, request, *args, **kwargs):
        self.comment_obj = get_object_or_404(
            Comment, id=request.POST.get("commentid"))
        if request.user == self.comment_obj.commented_by:
            form = LeadCommentForm(request.POST, instance=self.comment_obj)
            if form.is_valid():
                return self.form_valid(form)

            return self.form_invalid(form)

        data = {'error': "You don't have permission to edit this comment."}
        return JsonResponse(data)

    def form_valid(self, form):
        self.comment_obj.comment = form.cleaned_data.get("comment")
        self.comment_obj.save(update_fields=["comment"])
        comment_id = self.comment_obj.id
        current_site = get_current_site(self.request)
        send_email_user_mentions.delay(comment_id, 'leads', domain=current_site.domain,
            protocol=self.request.scheme)
        return JsonResponse({
            "commentid": self.comment_obj.id,
            "comment": self.comment_obj.comment,
        })

    def form_invalid(self, form):
        return JsonResponse({"error": form['comment'].errors})


class DeleteCommentView(LoginRequiredMixin, View):

    def post(self, request, *args, **kwargs):
        self.object = get_object_or_404(
            Comment, id=request.POST.get("comment_id"))
        if request.user == self.object.commented_by:
            self.object.delete()
            data = {"cid": request.POST.get("comment_id")}
            return JsonResponse(data)

        data = {'error': "You don't have permission to delete this comment."}
        return JsonResponse(data)


class GetLeadsView(LoginRequiredMixin, ListView):
    model = Lead
    context_object_name = "leads"
    template_name = "leads_list.html"


class AddAttachmentsView(LoginRequiredMixin, CreateView):
    model = Attachments
    form_class = LeadAttachmentForm
    http_method_names = ["post"]

    def post(self, request, *args, **kwargs):
        self.object = None
        self.lead = get_object_or_404(Lead, id=request.POST.get('leadid'))
        if (
                request.user in self.lead.assigned_to.all() or
                request.user == self.lead.created_by or
                request.user.is_superuser or
                request.user.role == 'ADMIN'
        ):
            form = self.get_form()
            if form.is_valid():
                return self.form_valid(form)
            return self.form_invalid(form)

        data = {'error': "You don't have permission to add attachment."}
        return JsonResponse(data)

    def form_valid(self, form):
        attachment = form.save(commit=False)
        attachment.created_by = self.request.user
        attachment.file_name = attachment.attachment.name
        attachment.lead = self.lead
        attachment.save()
        return JsonResponse({
            "attachment_id": attachment.id,
            "attachment": attachment.file_name,
            "attachment_url": attachment.attachment.url,
            "created_on": attachment.created_on,
            "created_by": attachment.created_by.email,
            "download_url": reverse(
                'common:download_attachment', kwargs={'pk': attachment.id}),
            "attachment_display": attachment.get_file_type_display(),
            "file_type": attachment.file_type()
        })

    def form_invalid(self, form):
        return JsonResponse({"error": form['attachment'].errors})


class DeleteAttachmentsView(LoginRequiredMixin, View):

    def post(self, request, *args, **kwargs):
        self.object = get_object_or_404(
            Attachments, id=request.POST.get("attachment_id"))
        if (
            request.user == self.object.created_by or
            request.user.is_superuser or
            request.user.role == 'ADMIN'
        ):
            self.object.delete()
            data = {"aid": request.POST.get("attachment_id")}
            return JsonResponse(data)

        data = {'error':
                "You don't have permission to delete this attachment."}
        return JsonResponse(data)


def create_lead_from_site(request):
    if request.method == "POST":
        # ip_addres = get_client_ip(request)
        # website_address = request.scheme + '://' + ip_addres
        api_key = request.POST.get('apikey')
        # api_setting = APISettings.objects.filter(
        #     website=website_address, apikey=api_key).first()
        api_setting = APISettings.objects.filter(apikey=api_key).first()
        if not api_setting:
            return JsonResponse({
                'error': True,
                'message':
                "You don't have permission, please contact the admin!."},
                status=status.HTTP_400_BAD_REQUEST)

        if (api_setting and request.POST.get("email") and
                request.POST.get("full_name")):
            # user = User.objects.filter(is_admin=True, is_active=True).first()
            user = api_setting.created_by
            lead = Lead.objects.create(
                title=request.POST.get("full_name"),
                status="assigned", source=api_setting.website,
                description=request.POST.get("message"),
                email=request.POST.get("email"),
                phone=request.POST.get("phone"),
                is_active=True, created_by=user)
            lead.assigned_to.add(user)
            # Send Email to Assigned Users
            site_address = request.scheme + '://' + request.META['HTTP_HOST']
            send_lead_assigned_emails.delay(lead.id, [user.id], site_address)
            # Create Contact
            contact = Contact.objects.create(
                first_name=request.POST.get("full_name"),
                email=request.POST.get("email"),
                phone=request.POST.get("phone"),
                description=request.POST.get("message"), created_by=user,
                is_active=True)
            contact.assigned_to.add(user)

            lead.contacts.add(contact)

            return JsonResponse({'error': False,
                                 'message': "Lead Created sucessfully."},
                                status=status.HTTP_201_CREATED)
        return JsonResponse({'error': True, 'message': "In-valid data."},
                            status=status.HTTP_400_BAD_REQUEST)
    return JsonResponse({
        'error': True, 'message': "In-valid request method."},
        status=status.HTTP_400_BAD_REQUEST)
