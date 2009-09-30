from django import forms
from django.conf.urls.defaults import patterns, url
from django.contrib import admin
from django.contrib.contenttypes.models import ContentType
from django.core.urlresolvers import reverse
from django.http import HttpResponseRedirect, HttpResponse, Http404
from django.template import RequestContext, loader

from gatekeeper.models import ModeratedObject, STATUS_CHOICES

class StatusChoicesForm(forms.Form):
    status = forms.ChoiceField(choices=STATUS_CHOICES, required=True,
        help_text='''Select the updated moderation status''')

class ModeratedObjectAdmin(admin.ModelAdmin):
    list_display = ['self_unicode', 'content_object', 'content_type', 'moderation_status', 'timestamp']
    list_filter = ['moderation_status','flagged', 'content_type']
    actions = ['batch_change_status_action']
    
    def batch_change_status_action(self, request, queryset):
        """
            Allows for batch status change of moderated objects as a replacement
            to the previous additional views.
        """
        ct = ContentType.objects.get_for_model(queryset.model)
        selected = request.POST.getlist(admin.ACTION_CHECKBOX_NAME)
        return HttpResponseRedirect('%s?ct=%s&ids=%s' % (
                reverse('admin:admin_gatekeeper_moderated_object_batch_change_status'),
                ct.pk, ",".join(selected)))
    batch_change_status_action.short_description = u"Change Status"
    
    def get_urls(self):
        base_urls = super(ModeratedObjectAdmin, self).get_urls()
        custom_urls = patterns('',
            url(r'^batch_change_status/$',
                self.admin_site.admin_view(self.batch_change_status),
                name = u'admin_gatekeeper_moderated_object_batch_change_status'),
        )
        return custom_urls + base_urls
    
    def batch_change_status(self, request):
        # Get the content type, and ID set & corresponding objects
        try:
            ct = ContentType.objects.get(pk=request.GET.get(u'ct', -1))
            ids = request.GET.get(u'ids').split(',')
            objects = ct.model_class()._default_manager.filter(pk__in=ids)
        except ContentType.DoesNotExist:
            raise Http404
        
        ## Validate GET data
        # The content type should only ever be an ModeratedObject
        if ct != ContentType.objects.get(app_label=ModeratedObject._meta.app_label,
                                         model=ModeratedObject._meta.module_name):
            raise Http404 # TODO: Use a better error
        
        # Form the redirect URL
        redir_url = u'%s:%s_%s_changelist' % (
                        self.admin_site.name,
                        ct.model_class()._meta.app_label,
                        ct.model_class()._meta.module_name)
        
        form = StatusChoicesForm()
        
        if request.method == "POST":
            form = StatusChoicesForm(request.POST)
            if form.is_valid():
                status = form.cleaned_data[u'status']
                for obj in objects:
                    obj._moderate(status, request.user)
                request.user.message_set.create(
                    message = u'%s objects successfully changed status to %s.' % (
                        objects.count(),
                        status,
                    )
                )
                return HttpResponseRedirect(reverse(redir_url))
        
        t = loader.get_template(u'admin/gatekeeper/batch_change_status.html')
        c = RequestContext(request, {
            'ct_opts':     ct.model_class()._meta,
            'entity_opts': ModeratedObject._meta,
            'obj_count': objects.count(),
            'form': form,
            'media': form.media,
        })
        return HttpResponse(t.render(c))

admin.site.register(ModeratedObject, ModeratedObjectAdmin)

if not admin.site.index_template:
    admin.site.index_template = "admin/gatekeeper/index.html"
