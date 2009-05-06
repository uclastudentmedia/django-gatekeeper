__author__ = "Jeremy Carbaugh (jcarbaugh@sunlightfoundation.com)"
__version__ = "0.1"
__copyright__ = "Copyright (c) 2008 Sunlight Labs"
__license__ = "BSD"

from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.contrib.sites.models import Site
from django.core.mail import send_mail
from django.core.urlresolvers import reverse
from django.db.models import Manager, signals
from django.dispatch import Signal
from gatekeeper.middleware import get_current_user
from gatekeeper.models import ModeratedObject
import datetime

REJECTED_STATUS = -1
PENDING_STATUS  = 0
APPROVED_STATUS = 1

ENABLE_AUTOMODERATION = getattr(settings, "GATEKEEPER_ENABLE_AUTOMODERATION", False)
DEFAULT_STATUS = getattr(settings, "GATEKEEPER_DEFAULT_STATUS", PENDING_STATUS)
MODERATOR_LIST = getattr(settings, "GATEKEEPER_MODERATOR_LIST", [])
GATEKEEPER_TABLE = ModeratedObject._meta.db_table

post_moderation = Signal(providing_args=["instance"])
post_flag = Signal(providing_args=["instance"])

def _get_automod_user():
    try:
        return User.objects.get(username__exact="gatekeeper_automod")
    except User.DoesNotExist:
        from django.contrib.sites.models import Site
        site = Site.objects.get(id=settings.SITE_ID)
        automod_user = User.objects.create_user(
            'gatekeeper_automod', 'gatekeeper_automod@%s' % site.domain)
        automod_user.save()
        return automod_user

# Register Models with Gatekeeper
registered_models = {}

def register(model, import_unmoderated=False, auto_moderator=None, 
             manager_name='objects', status_name='moderation_status',
             flagged_name='flagged', moderation_object_name='moderation_object',
             base_manager=None):
    if not model in registered_models:
        signals.post_save.connect(save_handler, sender=model)
        signals.pre_delete.connect(delete_handler, sender=model)
        # pass extra params onto add_fields to define what fields are named
        add_fields(model, manager_name, status_name, flagged_name, 
                   moderation_object_name, base_manager)
        registered_models[model] = auto_moderator
        if import_unmoderated:
            try:
                mod_obj_ids = model.objects.all().values_list('pk', flat=True)
                unmod_objs = model._default_manager.exclude(pk__in=mod_obj_ids)
                print 'importing %s unmoderated objects...' % unmod_objs.count()
                for obj in unmod_objs:
                    mo = ModeratedObject(
                        moderation_status=DEFAULT_STATUS,
                        content_object=obj,
                        timestamp=datetime.datetime.now())
                    mo.save()
            except:
                pass

# Add helper fields and custom manager to class
def add_fields(cls, manager_name, status_name, flagged_name,
               moderation_object_name, base_manager):
    
    # inherit from manager that is being replaced, fall back on models.Manager
    if base_manager is None:
        if hasattr(cls, manager_name):
            base_manager = getattr(cls, manager_name).__class__
        else:
            base_manager = Manager
    
    # queryset should inherit from manager's QuerySet
    base_queryset = base_manager().get_query_set().__class__
    
    class GatekeeperQuerySet(base_queryset):
        """ chainable queryset for checking status & flagging """
        
        def _by_status(self, field_name, status):
            where_clause = '%s = %%s' % (field_name)
            return self.extra(where=[where_clause], params=[status])
        
        def approved(self):
            return self._by_status(status_name, APPROVED_STATUS)
        
        def pending(self):
            return self._by_status(status_name, PENDING_STATUS)
        
        def rejected(self):
            return self._by_status(status_name, REJECTED_STATUS)
        
        def flagged(self):
            return self._by_status(flagged_name, 1)
        
        def not_flagged(self):
            return self._by_status(flagged_name, 0)
    
    class GatekeeperManager(base_manager):
        """ custom manager that adds parameters and uses custom QuerySet """
        
        # add moderation_id, status_name, and flagged_name attributes to the query
        def get_query_set(self):
            # parameters to help with generic SQL
            db_table = self.model._meta.db_table
            pk_name = self.model._meta.pk.attname
            content_type = ContentType.objects.get_for_model(self.model).id
            
            # extra params - status, flag, and id of object (for later access)
            select = {'_moderation_id':'%s.id' % GATEKEEPER_TABLE,
                      '_moderation_status':'%s.moderation_status' % GATEKEEPER_TABLE,
                      '_flagged':'%s.flagged' % GATEKEEPER_TABLE}
            where = ['content_type_id=%s' % content_type,
                     '%s.object_id=%s.%s' % (GATEKEEPER_TABLE, db_table, 
                                             pk_name)]
            tables=[GATEKEEPER_TABLE]
            
            # build extra query then copy model/query to a GatekeeperQuerySet
            q = super(GatekeeperManager, self).get_query_set().extra(
                select=select, where=where, tables=tables)
            return GatekeeperQuerySet(self.model, q.query)
    
    def _get_moderation_object(self):
        """ accessor for moderated_object that caches the object """
        if not hasattr(self, '_moderation_object'):
            self._moderation_object = ModeratedObject.objects.get(pk=self._moderation_id)
        return self._moderation_object
    
    # Add custom manager and helper fields to class
    cls.add_to_class(manager_name, GatekeeperManager())
    cls.add_to_class(moderation_object_name, property(_get_moderation_object))
    cls.add_to_class(status_name, property(lambda self: self._moderation_status))
    cls.add_to_class(flagged_name, property(lambda self: self._flagged))

# Handler for object creation/deletion
def save_handler(sender, **kwargs):
    if kwargs.get('created', None):
        instance = kwargs['instance']
        
        mo = ModeratedObject(
            moderation_status=DEFAULT_STATUS,
            content_object=instance,
            timestamp=datetime.datetime.now())
        mo.save()
        
        if ENABLE_AUTOMODERATION:
            auto_moderator = registered_models[instance.__class__]
            if auto_moderator:
                mod = auto_moderator(mo)
                if mod is None:
                    pass # ignore the moderator if it returns None
                elif mod:
                    mo.approve(_get_automod_user())
                else:
                    mo.reject(_get_automod_user())
            
            if mo.moderation_status == PENDING_STATUS: # if status is pending
                user = get_current_user()
                if user and user.is_authenticated():
                    if user.is_superuser or user.has_perm('gatekeeper.change_moderatedobject'):
                        mo.approve(user)
        
        if MODERATOR_LIST and mo.moderation_status < APPROVED_STATUS: # if there are moderators and the object is not approved
            subject = "[pending-moderation] %s" % instance
            message = "New object pending moderation.\n%s\nhttp://%s%s" % (instance, Site.objects.get_current().domain, reverse("admin_gatekeeper_moderated_object_batch_change_status"))
            from_addr = settings.DEFAULT_FROM_EMAIL 
            send_mail(subject, message, from_addr, MODERATOR_LIST, fail_silently=True)

def delete_handler(sender, **kwargs):
    instance = kwargs['instance']
    try:
        ct = ContentType.objects.get_for_model(sender)
        mo = ModeratedObject.objects.get(content_type=ct, object_id=instance.pk)
        mo.delete()
    except ModeratedObject.DoesNotExist:
        pass