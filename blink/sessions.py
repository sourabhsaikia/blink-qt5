
import bisect
import pickle as pickle
import contextlib
import io
import os
import re
import requests
import random
import string
import sys
import shutil
import uuid

from abc import ABCMeta, abstractproperty
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from enum import Enum
from itertools import chain
from operator import attrgetter
from dateutil.tz import tzlocal

from PyQt5 import uic
from PyQt5.QtCore import Qt, QAbstractListModel, QEasingCurve, QModelIndex, QObject, QProcess, QPropertyAnimation, pyqtSignal
from PyQt5.QtCore import QByteArray, QEvent, QMimeData, QPointF, QRect, QRectF, QSize, QTimer, QUrl, QFileInfo
from PyQt5.QtCore import QSettings
from PyQt5.QtGui import QBrush, QColor, QDesktopServices, QDrag, QIcon, QLinearGradient, QPainter, QPainterPath, QPalette, QPen, QPixmap, QPolygonF
from PyQt5.QtWidgets import QApplication, QAction, QDialog, QLabel, QListView, QMenu, QShortcut, QStyle, QStyledItemDelegate, QStyleOption, QFileIconProvider, QFileDialog

from application import log
from application.notification import IObserver, NotificationCenter, NotificationData, ObserverWeakrefProxy
from application.python import Null, limit
from application.python.types import MarkerType, Singleton
from application.python.weakref import weakobjectmap, defaultweakobjectmap
from application.system import makedirs, openfile, FileExistsError
from eventlib.proc import spawn
from zope.interface import implementer

from sipsimple.account import Account, AccountManager, BonjourAccount
from sipsimple.application import SIPApplication
from sipsimple.audio import AudioConference, WavePlayer
from sipsimple.configuration.datatypes import Path
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import SIPCoreError, SIPURI, ToHeader
from sipsimple.lookup import DNSLookup
from sipsimple.session import Session, IllegalStateError, IllegalDirectionError
from sipsimple.streams import MediaStreamRegistry
from sipsimple.streams.msrp.chat import OTRState, SMPStatus
from sipsimple.streams.msrp.filetransfer import FileSelector
from sipsimple.streams.msrp.screensharing import ExternalVNCServerHandler, ExternalVNCViewerHandler, ScreenSharingStream
from sipsimple.threading import run_in_thread, run_in_twisted_thread
from sipsimple.util import ISOTimestamp

from blink.logging import MessagingTrace as message_log
from blink.configuration.datatypes import File
from blink.configuration.settings import BlinkSettings
from blink.resources import ApplicationData, Resources
from blink.screensharing import ScreensharingWindow, VNCClient, ServerDefault
from blink.util import call_later, run_in_gui_thread, translate, copy_transfer_file
from blink.widgets.buttons import LeftSegment, MiddleSegment, RightSegment
from blink.widgets.labels import Status, StateColor
from blink.widgets.color import ColorHelperMixin, ColorUtils, cache_result, background_color_key
from blink.widgets.util import ContextMenuActions, QtDynamicProperty
from blink.widgets.zrtp import ZRTPWidget
from blink.streams.message import MessageStream

__all__ = ['ClientConference', 'ConferenceDialog', 'AudioSessionModel', 'AudioSessionListView', 'ChatSessionModel', 'ChatSessionListView', 'SessionManager']


translation_table = dict.fromkeys(map(ord, ' \t'), None)

class Container(object):
    pass


class RTPStreamInfo(object, metaclass=ABCMeta):
    dataset_size = 5000
    average_interval = 10

    def __init__(self):
        self.ice_status = None
        self.encryption = None
        self.encryption_cipher = None
        self.zrtp_sas = None
        self.zrtp_verified = False
        self.zrtp_peer_name = ''
        self.codec_name = None
        self.sample_rate = None
        self.local_address = None
        self.remote_address = None
        self.local_rtp_candidate = None
        self.remote_rtp_candidate = None
        self.latency = deque(maxlen=self.dataset_size)
        self.packet_loss = deque(maxlen=self.dataset_size)
        self.jitter = deque(maxlen=self.dataset_size)
        self.incoming_traffic = deque(maxlen=self.dataset_size)
        self.outgoing_traffic = deque(maxlen=self.dataset_size)
        self.bytes_sent = 0
        self.bytes_received = 0
        self._total_packets = 0
        self._total_packets_lost = 0
        self._total_packets_discarded = 0
        self._average_loss_queue = deque(maxlen=self.average_interval)

    @abstractproperty
    def codec(self):
        raise NotImplementedError

    def update(self, stream):
        if stream is not None:
            self.codec_name = stream.codec
            self.sample_rate = stream.sample_rate
            self.local_address = stream.local_rtp_address
            self.remote_address = stream.remote_rtp_address
            self.encryption = stream.encryption.type if stream.encryption.active else None
            self.encryption_cipher = stream.encryption.cipher if stream.encryption.active else None
            if self.encryption == 'ZRTP' and stream.encryption.zrtp and stream.encryption.zrtp.sas:
                self.zrtp_sas = stream.encryption.zrtp.sas.decode()
                self.zrtp_verified = stream.encryption.zrtp.verified
                self.zrtp_peer_name = stream.encryption.zrtp.peer_name
            if stream.session and not stream.session.account.nat_traversal.use_ice:
                self.ice_status = 'disabled'

    def update_statistics(self, statistics):
        if statistics:
            packets = statistics['rx']['packets'] - self._total_packets
            packets_lost = statistics['rx']['packets_lost'] - self._total_packets_lost
            packets_discarded = statistics['rx']['packets_discarded'] - self._total_packets_discarded
            self._average_loss_queue.append(100.0 * packets_lost / (packets + packets_lost - packets_discarded) if packets_lost else 0)
            self._total_packets += packets
            self._total_packets_lost += packets_lost
            self._total_packets_discarded += packets_discarded
            self.latency.append(statistics['rtt']['last'] / 1000 / 2)
            self.jitter.append(statistics['rx']['jitter']['last'] / 1000)
            self.incoming_traffic.append(float(statistics['rx']['bytes'] - self.bytes_received))  # bytes/second
            self.outgoing_traffic.append(float(statistics['tx']['bytes'] - self.bytes_sent))      # bytes/second
            self.bytes_sent = statistics['tx']['bytes']
            self.bytes_received = statistics['rx']['bytes']
            self.packet_loss.append(sum(self._average_loss_queue) / self.average_interval)

    def reset(self):
        self.__init__()


class MSRPStreamInfo(object, metaclass=ABCMeta):
    def __init__(self):
        self.local_address = None
        self.remote_address = None
        self.transport = None
        self.full_local_path = []
        self.full_remote_path = []

    def update(self, stream):
        if stream is not None:
            local_uri = stream.local_uri
            msrp_transport = stream.msrp
            if msrp_transport is not None:
                self.transport = stream.transport
                self.local_address = msrp_transport.local_uri.host
                self.remote_address = msrp_transport.next_host().host
                self.full_local_path = msrp_transport.full_local_path
                self.full_remote_path = msrp_transport.full_remote_path
            elif local_uri is not None:
                self.transport = stream.transport
                self.local_address = local_uri.host

    def reset(self):
        self.__init__()


class AudioStreamInfo(RTPStreamInfo):
    @property
    def codec(self):
        return '{} {}kHz'.format(self.codec_name.capitalize(), self.sample_rate//1000) if self.codec_name else None


class VideoStreamInfo(RTPStreamInfo):
    def __init__(self):
        super(VideoStreamInfo, self).__init__()
        self.framerate = None

    @property
    def codec(self):
        return '{} {}fps'.format(self.codec_name, int(self.framerate)) if self.codec_name else None

    def update(self, stream):
        super(VideoStreamInfo, self).update(stream)
        try:
            self.framerate = stream.producer.framerate
        except AttributeError:
            pass


class ChatStreamInfo(MSRPStreamInfo):
    def __init__(self):
        super(ChatStreamInfo, self).__init__()
        self.encryption = None
        self.encryption_cipher = None
        self.otr_key_fingerprint = None
        self.otr_peer_fingerprint = None
        self.otr_peer_name = ''
        self.otr_verified = False
        self.smp_status = SMPVerification.Unavailable

    def update(self, stream):
        super(ChatStreamInfo, self).update(stream)
        if stream is not None:
            self.encryption = 'OTR' if stream.encryption.active else None
            self.encryption_cipher = stream.encryption.cipher if stream.encryption.active else None
            if self.encryption == 'OTR':
                self.otr_key_fingerprint = stream.encryption.key_fingerprint.upper()
                self.otr_peer_fingerprint = stream.encryption.peer_fingerprint.upper()
                self.otr_peer_name = stream.encryption.peer_name
                self.otr_verified = stream.encryption.verified


class ScreenSharingStreamInfo(MSRPStreamInfo):
    def __init__(self):
        super(ScreenSharingStreamInfo, self).__init__()
        self.mode = None

    def update(self, stream):
        super(ScreenSharingStreamInfo, self).update(stream)
        if stream is not None:
            self.mode = stream.handler.type


class MessageStreamInfo(object):
    def __init__(self):
        self.encryption = None
        self.private_key = None
        self.encryption_cipher = None
        self.otr_key_fingerprint = None
        self.otr_peer_fingerprint = None
        self.otr_peer_name = ''
        self.otr_verified = False
        self.smp_status = SMPVerification.Unavailable

    def update(self, stream):
        if stream is not None:
            self.encryption = 'OTR' if stream.encryption.active else None
            self.encryption_cipher = stream.encryption.cipher if stream.encryption.active else None
            if self.encryption is None:
                self.encryption = 'OpenPGP' if stream.can_encrypt else None

            if self.encryption == 'OTR':
                self.otr_key_fingerprint = stream.encryption.key_fingerprint.upper()
                self.otr_peer_fingerprint = stream.encryption.peer_fingerprint.upper()
                self.otr_peer_name = stream.encryption.peer_name
                self.otr_verified = stream.encryption.verified

    def reset(self):
        pass


class StreamsInfo(object):
    __slots__ = 'audio', 'video', 'chat', 'screen_sharing', 'messages'

    def __init__(self):
        self.audio = AudioStreamInfo()
        self.video = VideoStreamInfo()
        self.chat = ChatStreamInfo()
        self.screen_sharing = ScreenSharingStreamInfo()
        self.messages = MessageStreamInfo()

    def __getitem__(self, key):
        key = key.replace('-', '_')
        try:
            return getattr(self, key)
        except AttributeError:
            raise KeyError(key)

    def update(self, streams, fake_streams):
        self.audio.update(streams.get('audio'))
        self.video.update(streams.get('video'))
        self.chat.update(streams.get('chat'))
        self.screen_sharing.update(streams.get('screen-sharing'))
        self.messages.update(fake_streams.get('messages'))


class SessionInfo(object):
    def __init__(self):
        self.duration = timedelta(0)
        self.local_address = None
        self.remote_address = None
        self.transport = None
        self.remote_user_agent = None
        self.streams = StreamsInfo()

    def update(self, session):
        sip_session = session.sip_session
        if sip_session is not None:
            self.transport = sip_session.transport
            self.local_address = session.account.contact[self.transport].host
            self.remote_address = sip_session.peer_address  # consider reading from sip_session.route if peer_address is None (route can also be None) -Dan
            self.remote_user_agent = sip_session.remote_user_agent
        self.streams.update(session.streams, session.fake_streams)


class StreamDescription(object):
    def __init__(self, type, **kw):
        self.type = type
        self.attributes = kw

    def create_stream(self):
        cls = MediaStreamRegistry.get(self.type)
        return cls(**self.attributes)

    def __repr__(self):
        if self.attributes:
            return "%s(%r, %s)" % (self.__class__.__name__, self.type, ', '.join("%s=%r" % pair for pair in self.attributes.items()))
        else:
            return "%s(%r)" % (self.__class__.__name__, self.type)


class StreamSet(object):
    def __init__(self, streams):
        self._stream_map = {stream.type: stream for stream in streams}

    def __getitem__(self, key):
        return self._stream_map[key]

    def __contains__(self, key):
        return key in self._stream_map or key in list(self._stream_map.values())

    def __iter__(self):
        return iter(sorted(list(self._stream_map.values()), key=attrgetter('type')))

    def __reversed__(self):
        return iter(sorted(list(self._stream_map.values()), key=attrgetter('type'), reverse=True))

    __hash__ = None

    def __len__(self):
        return len(self._stream_map)

    @property
    def types(self):
        return set(self._stream_map)

    def get(self, key, default=None):
        return self._stream_map.get(key, default)


class StreamMap(dict):
    def __init__(self):
        super(StreamMap, self).__init__()
        self.active_map = {}
        self.proposed_map = {}


class StreamContainerView(object):
    def __init__(self, session, stream_map):
        self._session = session
        self._stream_map = stream_map

    def __getitem__(self, key):
        return self._stream_map[key]

    def __contains__(self, key):
        return key in self._stream_map or key in list(self._stream_map.values())

    def __iter__(self):
        return iter(sorted(list(self._stream_map.values()), key=attrgetter('type')))

    def __reversed__(self):
        return iter(sorted(list(self._stream_map.values()), key=attrgetter('type'), reverse=True))

    __hash__ = None

    def __len__(self):
        return len(self._stream_map)

    @property
    def types(self):
        return set(self._stream_map)

    def get(self, key, default=None):
        return self._stream_map.get(key, default)


class StreamContainer(StreamContainerView):
    @property
    def active(self):
        return StreamContainerView(self._session, self._stream_map.active_map)

    @property
    def proposed(self):
        return StreamContainerView(self._session, self._stream_map.proposed_map)

    def add(self, stream):
        assert stream not in self
        stream.blink_session = self._session
        self._stream_map[stream.type] = self._stream_map.proposed_map[stream.type] = stream
        notification_center = NotificationCenter()
        notification_center.add_observer(self._session, sender=stream)

    def remove(self, stream):
        assert stream in self
        self._stream_map.pop(stream.type)
        self._stream_map.active_map.pop(stream.type, None)
        self._stream_map.proposed_map.pop(stream.type, None)
        notification_center = NotificationCenter()
        notification_center.remove_observer(self._session, sender=stream)

    def set_active(self, stream):
        assert stream in self
        self._stream_map.active_map[stream.type] = self._stream_map.proposed_map.pop(stream.type)

    def extend(self, iterable):
        for item in iterable:
            self.add(item)

    def clear(self):
        for stream in list(self._stream_map.values()):
            self.remove(stream)


class StreamListDescriptor(object):
    def __init__(self):
        self.values = defaultweakobjectmap(StreamMap)

    def __get__(self, instance, owner):
        if instance is None:
            return self
        return StreamContainer(instance, self.values[instance])

    def __set__(self, obj, value):
        raise AttributeError("Attribute cannot be set")

    def __delete__(self, obj):
        raise AttributeError("Attribute cannot be deleted")


class SessionState(str):
    state    = property(lambda self: str(self.partition('/')[0]) or None)
    substate = property(lambda self: str(self.partition('/')[2]) or None)

    def __eq__(self, other):
        if isinstance(other, SessionState):
            return self.state == other.state and self.substate == other.substate
        elif isinstance(other, str):
            state    = other.partition('/')[0] or None
            substate = other.partition('/')[2] or None
            if state == '*':
                return substate in ('*', None) or self.substate == substate
            elif substate == '*':
                return self.state == state
            else:
                return self.state == state and self.substate == substate
        return NotImplemented

    def __ne__(self, other):
        return not (self == other)


class SessionItemsDescriptor(object):
    class SessionItems(object):
        def __getattr__(self, name):
            return None

    def __init__(self):
        self.values = defaultweakobjectmap(self.SessionItems)

    def __get__(self, instance, owner):
        return self.values[instance] if instance is not None else None

    def __set__(self, obj, value):
        raise AttributeError("Attribute cannot be set")

    def __delete__(self, obj):
        raise AttributeError("Attribute cannot be deleted")


class BlinkSessionType(type):
    def __call__(cls, *args, **kw):
        instance = super(BlinkSessionType, cls).__call__(*args, **kw)
        instance.__establish__()
        return instance


class BlinkSessionBase(object, metaclass=BlinkSessionType):
    def __establish__(self):
        pass


@implementer(IObserver)
class BlinkSession(BlinkSessionBase):

    streams = StreamListDescriptor()
    items = SessionItemsDescriptor()
    fake_streams = StreamListDescriptor()

    def __init__(self):
        self._initialize()

    def __establish__(self):
        notification_center = NotificationCenter()
        notification_center.post_notification('BlinkSessionWasCreated', sender=self)

    def _initialize(self, reinitialize=False):
        if not reinitialize:
            self.state = None

            self.account = None
            self.contact = None
            self.contact_uri = None
            self.uri = None
            self.remote_instance_id = None
            self.server_conference = ServerConference(self)

            self._delete_when_done = False
            self._delete_requested = False

            self.timer = QTimer()
            self.timer.setInterval(1000)
            self.timer.timeout.connect(self._SH_TimerFired)
        else:
            self.timer.stop()


        self.direction = None
        self.__dict__['active'] = False

        self.lookup = None
        self.client_conference = None
        self.sip_session = None
        self.stream_descriptions = None
        self.streams.clear()

        self.local_hold = False
        self.remote_hold = False
        self.recording = False

        self.transfer_state = None
        self.transfer_direction = None

        self.info = SessionInfo()

        self._sibling = None

        self._smp_handler = Null
        self.routes = None
        self.chat_type = None
        self.last_failed_reason = None

    def _get_state(self):
        return self.__dict__['state']

    def _set_state(self, value):
        if value is not None and not isinstance(value, SessionState):
            value = SessionState(value)
        old_state = self.__dict__.get('state', None)
        new_state = self.__dict__['state'] = value
        if new_state != old_state:
            NotificationCenter().post_notification('BlinkSessionDidChangeState', sender=self, data=NotificationData(old_state=old_state, new_state=new_state))

    state = property(_get_state, _set_state)
    del _get_state, _set_state

    def _get_contact(self):
        return self.__dict__['contact']

    def _set_contact(self, value):
        old_contact = self.__dict__.get('contact', None)
        new_contact = self.__dict__['contact'] = value
        if new_contact != old_contact:
            notification_center = NotificationCenter()
            if old_contact is not None:
                notification_center.remove_observer(self, sender=old_contact)
            if new_contact is not None:
                notification_center.add_observer(self, sender=new_contact)

    contact = property(_get_contact, _set_contact)
    del _get_contact, _set_contact

    def _get_sip_session(self):
        return self.__dict__['sip_session']

    def _set_sip_session(self, value):
        old_session = self.__dict__.get('sip_session', None)
        new_session = self.__dict__['sip_session'] = value
        if new_session != old_session:
            notification_center = NotificationCenter()
            if old_session is not None:
                notification_center.remove_observer(self, sender=old_session)
            if new_session is not None:
                notification_center.add_observer(self, sender=new_session)

    sip_session = property(_get_sip_session, _set_sip_session)
    del _get_sip_session, _set_sip_session

    def _get_active(self):
        return self.__dict__['active']

    def _set_active(self, value):
        value = bool(value)
        if self.__dict__.get('active', None) == value:
            return
        self.__dict__['active'] = value
        if self.state in ('connecting/*', 'connected/*') and self.streams.types.intersection({'audio', 'video'}):
            entity = self.client_conference or self
            if value:
                entity.unhold()
            else:
                entity.hold()

    active = property(_get_active, _set_active)
    del _get_active, _set_active

    def _get_account(self):
        account_manager = AccountManager()
        account_id = self.__dict__.get('account', None)
        if account_id is not None:
            try:
                account = account_manager.get_account(account_id)
                if account.enabled:
                    return account
            except KeyError:
                pass
        account = account_manager.default_account
        self.__dict__['account'] = account.id
        return account

    def _set_account(self, account):
        self.__dict__['account'] = account.id if account is not None else None

    account = property(_get_account, _set_account)
    del _get_account, _set_account

    def _get_client_conference(self):
        return self.__dict__['client_conference']

    def _set_client_conference(self, value):
        old_conference = self.__dict__.get('client_conference', None)
        new_conference = self.__dict__['client_conference'] = value
        if old_conference is new_conference:
            return
        if old_conference is not None:
            old_conference.remove_session(self)
        if new_conference is not None:
            new_conference.add_session(self)
            self.unhold()
        elif not self.active:
            self.hold()
        notification_center = NotificationCenter()
        notification_center.post_notification('BlinkSessionDidChangeClientConference', sender=self, data=NotificationData(old_conference=old_conference, new_conference=new_conference))

    client_conference = property(_get_client_conference, _set_client_conference)
    del _get_client_conference, _set_client_conference

    def _get_chat_type(self):
        return self.__dict__['chat_type']

    def _set_chat_type(self, value):
        self.__dict__['chat_type'] = value

    chat_type = property(_get_chat_type, _set_chat_type)
    del _get_chat_type, _set_chat_type

    @property
    def persistent(self):
        return not self._delete_when_done and not self._delete_requested

    @property
    def reusable(self):
        return self.persistent and self.state in (None, 'initialized', 'ended')

    @property
    def duration(self):
        return self.info.duration

    @property
    def transport(self):
        return self.sip_session.transport if self.sip_session is not None else None

    @property
    def on_hold(self):
        return self.local_hold or self.remote_hold

    @property
    def remote_focus(self):
        return self.sip_session is not None and self.sip_session.remote_focus

    def updateTimestamp(self, timestamp=None):
        if self.items.chat:
            self.items.chat.timestamp = timestamp if timestamp else ISOTimestamp.now().replace(tzinfo=tzlocal())
            # TODO should we reorder the tiles?

    def init_incoming(self, sip_session, streams, contact, contact_uri, reinitialize=False, remote_instance_id=None):
        assert self.state in (None, 'initialized', 'ended')
        assert self.contact is None or contact.settings is self.contact.settings
        notification_center = NotificationCenter()
        if reinitialize:
            notification_center.post_notification('BlinkSessionWillReinitialize', sender=self)
            self._initialize(reinitialize=True)
        else:
            self._delete_when_done = len(streams) == 1 and streams[0].type == 'audio'
        self.direction = 'incoming'
        self.sip_session = sip_session
        self.account = sip_session.account
        self.contact = contact
        self.remote_instance_id = remote_instance_id
        self.contact_uri = contact_uri
        self.uri = self._parse_uri(contact_uri.uri)
        self.streams.extend(streams)
        self.info.update(self)
        self.state = 'connecting'
        if reinitialize:
            notification_center.post_notification('BlinkSessionDidReinitializeForIncoming', sender=self)
        else:
            notification_center.post_notification('BlinkSessionNewIncoming', sender=self)
        notification_center.post_notification('BlinkSessionInfoUpdated', sender=self, data=NotificationData(elements={'session', 'media', 'statistics'}))
        notification_center.post_notification('BlinkSessionConnectionProgress', sender=self, data=NotificationData(stage='connecting'))
        self.sip_session.accept(streams)

    def init_outgoing(self, account, contact, contact_uri, stream_descriptions, sibling=None, reinitialize=False, remote_instance_id=None):
        assert self.state in (None, 'initialized', 'ended')
        assert self.contact is None or contact.settings is self.contact.settings
        notification_center = NotificationCenter()
        if reinitialize:
            notification_center.post_notification('BlinkSessionWillReinitialize', sender=self)
            self._initialize(reinitialize=True)
        else:
            self._delete_when_done = len(stream_descriptions) == 1 and stream_descriptions[0].type == 'audio'

        self.direction = 'outgoing'
        self.account = account
        self.contact = contact
        self.contact_uri = contact_uri
        self.remote_instance_id = remote_instance_id
        self.uri = self._normalize_uri(contact_uri.uri)
        # reevaluate later, after we add the .active/.proposed attributes to streams, if creating the sip session and the streams at this point is desirable -Dan
        # note: creating the sip session early also need the test in hold/unhold/end to change from sip_session is (not) None to sip_session.state is (not) None -Dan
        self.stream_descriptions = StreamSet(stream_descriptions)
        for stream_description in self.stream_descriptions:
            if stream_description.type == 'chat':
                self.chat_type = 'MSRP'
            if stream_description.type == 'messages' and stream_description.type not in self.fake_streams:
                self.fake_streams.extend([stream_description.create_stream()])
        self._sibling = sibling
        self.state = 'initialized'
        self.info.update(self)
        if reinitialize:
            notification_center.post_notification('BlinkSessionDidReinitializeForOutgoing', sender=self)
        else:
            notification_center.post_notification('BlinkSessionNewOutgoing', sender=self)
        notification_center.post_notification('BlinkSessionInfoUpdated', sender=self, data=NotificationData(elements={'session', 'media', 'statistics'}))

    def init_transfer(self, sip_session, streams, contact, contact_uri, reinitialize=False):
        assert self.state in (None, 'initialized', 'ended')
        assert self.contact is None or contact.settings is self.contact.settings
        notification_center = NotificationCenter()
        if reinitialize:
            notification_center.post_notification('BlinkSessionWillReinitialize', sender=self)
            self._initialize(reinitialize=True)
        else:
            self._delete_when_done = len(streams) == 1 and streams[0].type == 'audio'
        self.direction = 'outgoing'
        self.sip_session = sip_session
        self.account = sip_session.account
        self.contact = contact
        self.contact_uri = contact_uri
        self.uri = self._normalize_uri(contact_uri.uri)
        self.stream_descriptions = StreamSet(StreamDescription(stream.type) for stream in streams)

        self.state = 'initialized'
        if reinitialize:
            notification_center.post_notification('BlinkSessionDidReinitializeForOutgoing', sender=self)
        else:
            notification_center.post_notification('BlinkSessionNewOutgoing', sender=self)

        self.streams.extend(streams)

        self.info.update(self)
        notification_center.post_notification('BlinkSessionInfoUpdated', sender=self, data=NotificationData(elements={'session', 'media', 'statistics'}))

        self.state = 'connecting/dns_lookup'
        notification_center.post_notification('BlinkSessionWillConnect', sender=self, data=NotificationData(sibling=None))
        notification_center.post_notification('BlinkSessionConnectionProgress', sender=self, data=NotificationData(stage='dns_lookup'))

        self.stream_descriptions = None

        self.state = 'initializing'
        notification_center.post_notification('BlinkSessionConnectionProgress', sender=self, data=NotificationData(stage='connecting'))

    def connect(self):
        assert self.direction == 'outgoing' and self.state == 'initialized'
        notification_center = NotificationCenter()
        self.streams.extend(stream_description.create_stream() for stream_description in self.stream_descriptions)
        self.state = 'connecting/dns_lookup'
        notification_center.post_notification('BlinkSessionWillConnect', sender=self, data=NotificationData(sibling=self._sibling))
        self.stream_descriptions = None
        self._sibling = None
        notification_center.post_notification('BlinkSessionConnectionProgress', sender=self, data=NotificationData(stage='dns_lookup'))
        account = self.account
        settings = SIPSimpleSettings()
        if isinstance(account, Account):
            if account.sip.outbound_proxy is not None:
                proxy = account.sip.outbound_proxy
                uri = SIPURI(host=proxy.host, port=proxy.port, parameters={'transport': proxy.transport})
            elif account.sip.always_use_my_proxy:
                uri = SIPURI(host=account.id.domain)
            else:
                uri = self.uri
        else:
            uri = self.uri

        self.lookup = DNSLookup()
        notification_center.add_observer(self, sender=self.lookup)
        self.lookup.lookup_sip_proxy(uri, settings.sip.transport_list, tls_name=self.account.sip.tls_name or uri.host)

    def add_stream(self, stream_description):
        if stream_description.type == 'messages' and stream_description.type not in self.fake_streams:
            self.fake_streams.extend([stream_description.create_stream()])
        self.add_streams([stream_description])

    def add_streams(self, stream_descriptions):
        assert self.state == 'connected'
        for stream_description in stream_descriptions:
            if stream_description.type in self.streams:
                raise RuntimeError('session already has a stream of type %s' % stream_description.type)
            self.info.streams[stream_description.type].reset()
        streams = [stream_description.create_stream() for stream_description in stream_descriptions]
        self.sip_session.add_streams(streams)
        self.streams.extend(streams)
        notification_center = NotificationCenter()
        for stream in streams:
            notification_center.post_notification('BlinkSessionWillAddStream', sender=self, data=NotificationData(stream=stream))
            notification_center.post_notification('BlinkSessionInfoUpdated', sender=self, data=NotificationData(elements={'media', 'statistics'}))

    def remove_stream(self, stream):
        self.remove_streams([stream])

    def remove_streams(self, streams):
        assert self.state == 'connected'
        if not set(self.streams).issuperset(streams):
            raise RuntimeError('not all streams are part of the session')
        self.sip_session.remove_streams(streams)
        notification_center = NotificationCenter()
        for stream in streams:
            notification_center.post_notification('BlinkSessionWillRemoveStream', sender=self, data=NotificationData(stream=stream))

    def accept_proposal(self, streams):
        assert self.state == 'connected/received_proposal'
        self.sip_session.accept_proposal(streams)
        notification_center = NotificationCenter()
        for stream in streams:
            self.info.streams[stream.type].reset()
            notification_center.post_notification('BlinkSessionWillAddStream', sender=self, data=NotificationData(stream=stream))
        notification_center.post_notification('BlinkSessionInfoUpdated', sender=self, data=NotificationData(elements={'media', 'statistics'}))

    def hold(self):
        if self.sip_session is not None and not self.local_hold:
            self.local_hold = True
            self.sip_session.hold()
            NotificationCenter().post_notification('BlinkSessionDidChangeHoldState', sender=self, data=NotificationData(originator='local', local_hold=self.local_hold, remote_hold=self.remote_hold))

    def unhold(self):
        if self.sip_session is not None and self.local_hold:
            self.local_hold = False
            self.sip_session.unhold()
            NotificationCenter().post_notification('BlinkSessionDidChangeHoldState', sender=self, data=NotificationData(originator='local', local_hold=self.local_hold, remote_hold=self.remote_hold))

    def send_dtmf(self, digit):
        audio_stream = self.streams.get('audio')
        if audio_stream is None:
            return
        try:
            audio_stream.send_dtmf(digit)
        except RuntimeError:
            pass
        else:
            digit_map = {'*': 'star'}
            filename = 'sounds/dtmf_%s_tone.wav' % digit_map.get(digit, digit)
            player = WavePlayer(SIPApplication.voice_audio_bridge.mixer, Resources.get(filename))
            if self.account.rtp.inband_dtmf:
                audio_stream.bridge.add(player)
            SIPApplication.voice_audio_bridge.add(player)
            player.start()

    def start_recording(self):
        audio_stream = self.streams.get('audio')
        if audio_stream is not None and not self.recording:
            settings = SIPSimpleSettings()
            direction = self.sip_session.direction
            remote = "%s@%s" % (self.sip_session.remote_identity.uri.user, self.sip_session.remote_identity.uri.host)
            filename = "%s-%s-%s.wav" % (datetime.now().strftime("%Y%m%d-%H%M%S"), remote, direction)
            path = os.path.join(settings.audio.recordings_directory.normalized, self.account.id)
            try:
                audio_stream.start_recording(os.path.join(path, filename))
            except (SIPCoreError, IOError, OSError) as e:
                print('Failed to record: %s' % e)
            else:
                self.recording = True

    def stop_recording(self):
        audio_stream = self.streams.get('audio')
        if audio_stream is not None:
            self.recording = False
            audio_stream.stop_recording()

    def transfer(self, contact_uri, replaced_session=None):
        if self.state != 'connected':
            return
        replaced_sip_session = None if replaced_session is None else replaced_session.sip_session
        try:
            self.sip_session.transfer(self._parse_uri(contact_uri.uri), replaced_session=replaced_sip_session)
        except (IllegalStateError, IllegalDirectionError) as e:
            log.error("Call transfer error: %s" % str(e))
            pass

    def end(self, delete=False):
        if self.state == 'ending':
            self._delete_requested = delete
        elif self.state == 'ended':
            self._delete_requested = delete
            if delete:
                self._delete()
        elif self.state in ('initialized', 'connecting/*', 'connected/*'):
            self._delete_requested = delete
            self.state = 'ending'
            notification_center = NotificationCenter()
            notification_center.post_notification('BlinkSessionWillEnd', sender=self)
            if self.sip_session is None:
                self._terminate(reason='Call cancelled', error=True)
            else:
                self.sip_session.end()

    def _delete(self):
        if self.state != 'ended':
            return
        self.state = 'deleted'

        notification_center = NotificationCenter()
        notification_center.post_notification('BlinkSessionWasDeleted', sender=self)

        self.account = None
        self.contact = None
        self.contact_uri = None

    def _terminate(self, reason, error=False):
        notification_center = NotificationCenter()

        if self.state != 'ending':
            self.state = 'ending'
            notification_center.post_notification('BlinkSessionWillEnd', sender=self)

        self.timer.stop()
        self.streams.clear()

        self.lookup = None
        self.sip_session = None
        self.stream_descriptions = None
        self._sibling = None

        self.local_hold = False
        self.remote_hold = False
        self.recording = False

        state = SessionState('ended')
        state.reason = reason
        state.error = error

        self.state = state
        notification_center.post_notification('BlinkSessionDidEnd', sender=self, data=NotificationData(reason=reason, error=error))

        if not self.persistent:
            self._delete()

    def _parse_uri(self, uri):
        if '@' not in uri:
            uri += '@' + self.account.id.domain
        if not uri.startswith(('sip:', 'sips:')):
            uri = 'sip:' + uri
        return SIPURI.parse(str(uri))

    def _normalize_uri(self, uri):
        from blink.contacts import URIUtils

        if '@' not in uri:
            uri += '@' + self.account.id.domain
        if not uri.startswith(('sip:', 'sips:')):
            uri = 'sip:' + uri

        uri = SIPURI.parse(str(uri).translate(translation_table))
        if URIUtils.is_number(uri.user.decode()):
            user = URIUtils.trim_number(uri.user.decode())
            if isinstance(self.account, Account):
                if self.account.pstn.idd_prefix is not None:
                    user = re.sub(r'^\+', self.account.pstn.idd_prefix, user)
                if self.account.pstn.prefix is not None:
                    user = self.account.pstn.prefix + user
            uri.user = user.encode()
        return uri

    def _sync_chat_peer_name(self):
        chat_stream = self.streams.active.get('chat', Null)
        if chat_stream.encryption.active and chat_stream.encryption.peer_name == '':
            chat_stream.encryption.peer_name = self.info.streams.audio.zrtp_peer_name

    def _SH_TimerFired(self):
        self.info.duration += timedelta(seconds=1)
        self.info.streams.audio.update_statistics(self.streams.get('audio', Null).statistics)
        self.info.streams.video.update_statistics(self.streams.get('video', Null).statistics)
        notification_center = NotificationCenter()
        notification_center.post_notification('BlinkSessionInfoUpdated', sender=self, data=NotificationData(elements={'statistics'}))

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_DNSLookupDidSucceed(self, notification):
        notification.center.remove_observer(self, sender=notification.sender)
        if notification.sender is self.lookup:
            routes = notification.data.result
            if routes:
                self.routes = routes
                self.state = 'connection/dns_lookup_succeeded'
                self.info.update(self)
                self.sip_session = Session(self.account)
                self.sip_session.connect(ToHeader(self.uri), routes, list(self.streams))
            else:
                self.routes = None
                self._terminate(reason='Domain not found in DNS', error=True)

    def _NH_DNSLookupDidFail(self, notification):
        notification.center.remove_observer(self, sender=notification.sender)
        if notification.sender is self.lookup:
            self._terminate(reason='Domain not found in DNS', error=True)

    def _NH_SIPSessionNewOutgoing(self, notification):
        self.state = 'connecting'
        self.info.update(self)
        notification.center.post_notification('BlinkSessionConnectionProgress', sender=self, data=NotificationData(stage='connecting'))
        notification.center.post_notification('BlinkSessionInfoUpdated', sender=self, data=NotificationData(elements={'session'}))

    def _NH_SIPSessionGotProvisionalResponse(self, notification):
        if notification.data.code == 180:
            self.state = 'connecting/ringing'
            notification.center.post_notification('BlinkSessionConnectionProgress', sender=self, data=NotificationData(stage='ringing'))
        elif notification.data.code == 183:
            self.state = 'connecting/early_media'
            notification.center.post_notification('BlinkSessionConnectionProgress', sender=self, data=NotificationData(stage='early_media'))
        self.info.update(self)
        notification.center.post_notification('BlinkSessionInfoUpdated', sender=self, data=NotificationData(elements={'session', 'media'}))

    def _NH_SIPSessionWillStart(self, notification):
        self.state = 'connecting/starting'
        self.info.update(self)
        notification.center.post_notification('BlinkSessionConnectionProgress', sender=self, data=NotificationData(stage='starting'))
        notification.center.post_notification('BlinkSessionInfoUpdated', sender=self, data=NotificationData(elements={'session', 'media'}))

    def _NH_SIPSessionDidStart(self, notification):
        for stream in set(self.streams).difference(notification.data.streams):
            self.streams.remove(stream)
        for stream in self.streams:
            self.streams.set_active(stream)
        if self.state not in ('ending', 'ended', 'deleted'):
            self.state = 'connected'
            self.timer.start()
            self.info.update(self)
            notification.center.post_notification('BlinkSessionDidConnect', sender=self)
            notification.center.post_notification('BlinkSessionInfoUpdated', sender=self, data=NotificationData(elements={'session', 'media'}))

    def _NH_SIPSessionDidFail(self, notification):
        if notification.data.failure_reason == 'user request':
            if notification.data.code == 487:
                reason = 'Call cancelled'
            else:
                reason = notification.data.reason or 'Call failed'
        else:
            reason = notification.data.failure_reason
        self._terminate(reason=reason, error=True)

    def _NH_SIPSessionDidEnd(self, notification):
        self._terminate(translate('sessions', 'Call ended') if notification.data.originator == 'local' else translate('sessions', 'Call ended by remote'))

    def _NH_SIPSessionDidChangeHoldState(self, notification):
        if notification.data.originator == 'remote':
            self.remote_hold = notification.data.on_hold
        notification.center.post_notification('BlinkSessionDidChangeHoldState', sender=self, data=NotificationData(originator=notification.data.originator, local_hold=self.local_hold, remote_hold=self.remote_hold))

    def _NH_SIPSessionNewProposal(self, notification):
        if self.state not in ('ending', 'ended', 'deleted'):
            if notification.data.originator == 'local':
                self.state = 'connected/sent_proposal'
            else:
                for stream in (stream for stream in notification.data.proposed_streams if stream.type not in self.streams):
                    self.streams.add(stream)
                self.state = 'connected/received_proposal'

    def _NH_SIPSessionProposalAccepted(self, notification):
        accepted_streams = notification.data.accepted_streams
        proposed_streams = notification.data.proposed_streams
        for stream in proposed_streams:
            if stream in accepted_streams:
                self.streams.set_active(stream)
                notification.center.post_notification('BlinkSessionDidAddStream', sender=self, data=NotificationData(stream=stream))
            else:
                self.streams.remove(stream)
                if self.state == 'connected/sent_proposal':
                    notification.center.post_notification('BlinkSessionDidNotAddStream', sender=self, data=NotificationData(stream=stream))
        if self.state not in ('ending', 'ended', 'deleted'):
            self.state = 'connected'
        if accepted_streams:
            self.info.streams.update(self.streams, self.fake_streams)
            notification.center.post_notification('BlinkSessionInfoUpdated', sender=self, data=NotificationData(elements={'media'}))

    def _NH_SIPSessionProposalRejected(self, notification):
        for stream in set(notification.data.proposed_streams).intersection(self.streams):
            self.streams.remove(stream)
            if self.state == 'connected/sent_proposal':
                notification.data.stream = stream
                notification.center.post_notification('BlinkSessionDidNotAddStream', sender=self, data=notification.data)
        if self.state not in ('ending', 'ended', 'deleted'):
            self.state = 'connected'

    def _NH_SIPSessionHadProposalFailure(self, notification):
        for stream in set(notification.data.proposed_streams).intersection(self.streams):
            self.streams.remove(stream)
            notification.center.post_notification('BlinkSessionDidNotAddStream', sender=self, data=NotificationData(stream=stream))
        if self.state not in ('ending', 'ended', 'deleted'):
            self.state = 'connected'

    def _NH_SIPSessionDidRenegotiateStreams(self, notification):
        if notification.data.added_streams:
            self._delete_when_done = False
        for stream in set(notification.data.removed_streams).intersection(self.streams):
            self.streams.remove(stream)
            notification.center.post_notification('BlinkSessionDidRemoveStream', sender=self, data=NotificationData(stream=stream))
        if not self.streams:
            self.end()
        elif self.streams.types.isdisjoint({'audio', 'video'}):
            self.unhold()

    def _NH_SIPSessionTransferNewIncoming(self, notification):
        self.transfer_state = 'active'
        self.transfer_direction = 'incoming'
        notification.center.post_notification('BlinkSessionTransferNewIncoming', sender=self, data=notification.data)

    def _NH_SIPSessionTransferNewOutgoing(self, notification):
        self.transfer_state = 'active'
        self.transfer_direction = 'outgoing'
        notification.center.post_notification('BlinkSessionTransferNewOutgoing', sender=self, data=notification.data)

    def _NH_SIPSessionTransferDidStart(self, notification):
        notification.center.post_notification('BlinkSessionTransferDidStart', sender=self, data=notification.data)

    def _NH_SIPSessionTransferDidEnd(self, notification):
        self.transfer_state = 'completed'
        notification.center.post_notification('BlinkSessionTransferDidEnd', sender=self, data=notification.data)

    def _NH_SIPSessionTransferDidFail(self, notification):
        self.transfer_state = 'failed'
        notification.center.post_notification('BlinkSessionTransferDidFail', sender=self, data=notification.data)

    def _NH_SIPSessionTransferGotProgress(self, notification):
        notification.center.post_notification('BlinkSessionTransferGotProgress', sender=self, data=notification.data)

    def _NH_MediaStreamDidStart(self, notification):
        stream = notification.sender
        audio_stream = self.streams.get('audio')
        if stream.type == 'chat' and stream.session.remote_focus and 'com.ag-projects.zrtp-sas' in stream.chatroom_capabilities and audio_stream is not None:
            secure_chat = stream.transport == 'tls' and all(len(path) == 1 for path in (stream.msrp.full_local_path, stream.msrp.full_remote_path))  # tls & direct connection
            if audio_stream.encryption.type == 'ZRTP' and audio_stream.encryption.zrtp.sas is not None and not audio_stream.encryption.zrtp.verified and secure_chat:
                stream.send_message(audio_stream.encryption.zrtp.sas, 'application/blink-zrtp-sas')
        if stream.type == 'chat':
            self.chat_type = 'MSRP'

    def _NH_MediaStreamWillEnd(self, notification):
        if notification.sender.type == 'chat':
            self.chat_type = ''
            self._smp_handler.stop()
            self._smp_handler = Null

    def _NH_RTPStreamICENegotiationStateDidChange(self, notification):
        if notification.data.state in {'GATHERING', 'GATHERING_COMPLETE', 'NEGOTIATING'}:
            stream_info = self.info.streams[notification.sender.type]
            stream_info.ice_status = notification.data.state.lower()
            notification.center.post_notification('BlinkSessionInfoUpdated', sender=self, data=NotificationData(elements={'media'}))

    def _NH_RTPStreamICENegotiationDidSucceed(self, notification):
        stream_info = self.info.streams[notification.sender.type]
        stream_info.ice_status = 'succeeded'
        stream_info.local_rtp_candidate = notification.sender.local_rtp_candidate
        stream_info.remote_rtp_candidate = notification.sender.remote_rtp_candidate
        notification.center.post_notification('BlinkSessionInfoUpdated', sender=self, data=NotificationData(elements={'media'}))

    def _NH_RTPStreamICENegotiationDidFail(self, notification):
        stream_info = self.info.streams[notification.sender.type]
        stream_info.ice_status = 'failed'
        notification.center.post_notification('BlinkSessionInfoUpdated', sender=self, data=NotificationData(elements={'media'}))

    def _NH_AudioStreamGotDTMF(self, notification):
        digit_map = {'*': 'star'}
        filename = 'sounds/dtmf_%s_tone.wav' % digit_map.get(notification.data.digit, notification.data.digit)
        player = WavePlayer(SIPApplication.voice_audio_bridge.mixer, Resources.get(filename))
        SIPApplication.voice_audio_bridge.add(player)
        player.start()

    def _NH_AudioStreamDidStartRecording(self, notification):
        self.recording = True
        notification.center.post_notification('BlinkSessionDidChangeRecordingState', sender=self, data=NotificationData(recording=self.recording))

    def _NH_AudioStreamWillStopRecording(self, notification):
        self.recording = False
        notification.center.post_notification('BlinkSessionDidChangeRecordingState', sender=self, data=NotificationData(recording=self.recording))

    def _NH_RTPStreamZRTPReceivedSAS(self, notification):
        stream = notification.sender
        self.info.streams[stream.type].update(stream)
        notification.center.post_notification('BlinkSessionInfoUpdated', sender=self, data=NotificationData(elements={'media'}))
        chat_stream = self.streams.get('chat')
        if stream.session.remote_focus and not notification.data.verified and chat_stream is not None and 'com.ag-projects.zrtp-sas' in chat_stream.chatroom_capabilities:
            msrp_transport = chat_stream.msrp
            if msrp_transport is not None:
                secure_chat = chat_stream.transport == 'tls' and all(len(path) == 1 for path in (msrp_transport.full_local_path, msrp_transport.full_remote_path))  # tls & direct connection
                if secure_chat:
                    chat_stream.send_message(notification.data.sas, 'application/blink-zrtp-sas')
        self._sync_chat_peer_name()
        self._smp_handler.handle_notification(notification)

    def _NH_RTPStreamZRTPVerifiedStateChanged(self, notification):
        self.info.streams[notification.sender.type].update(notification.sender)
        notification.center.post_notification('BlinkSessionInfoUpdated', sender=self, data=NotificationData(elements={'media'}))
        self._smp_handler.handle_notification(notification)

    def _NH_RTPStreamZRTPPeerNameChanged(self, notification):
        self.info.streams[notification.sender.type].update(notification.sender)
        notification.center.post_notification('BlinkSessionInfoUpdated', sender=self, data=NotificationData(elements={'media'}))
        self._sync_chat_peer_name()

    def _NH_RTPStreamDidEnableEncryption(self, notification):
        self.info.streams[notification.sender.type].update(notification.sender)
        notification.center.post_notification('BlinkSessionInfoUpdated', sender=self, data=NotificationData(elements={'media'}))

    def _NH_RTPStreamDidNotEnableEncryption(self, notification):
        self.info.streams[notification.sender.type].update(notification.sender)
        notification.center.post_notification('BlinkSessionInfoUpdated', sender=self, data=NotificationData(elements={'media'}))

    def _NH_VideoStreamRemoteFormatDidChange(self, notification):
        self.info.streams.video.update(notification.sender)
        notification.center.post_notification('BlinkSessionInfoUpdated', sender=self, data=NotificationData(elements={'media'}))

    def _NH_ChatStreamOTREncryptionStateChanged(self, notification):
        self.info.streams.chat.update(notification.sender)
        if self.chat_type is None:
            self.info.streams.messages.update(notification.sender)
        notification.center.post_notification('BlinkSessionInfoUpdated', sender=self, data=NotificationData(elements={'media'}))
        if notification.data.new_state is OTRState.Encrypted:
            self._smp_handler = SMPVerificationHandler(self)
            self._smp_handler.start()
        elif notification.data.old_state is OTRState.Encrypted:
            self._smp_handler.stop()
            self._smp_handler = Null
        self._sync_chat_peer_name()

    def _NH_ChatStreamOTRVerifiedStateChanged(self, notification):
        self.info.streams.chat.update(notification.sender)
        if self.chat_type is None:
            self.info.streams.messages.update(notification.sender)
        notification.center.post_notification('BlinkSessionInfoUpdated', sender=self, data=NotificationData(elements={'media'}))

    def _NH_ChatStreamOTRPeerNameChanged(self, notification):
        self.info.streams.chat.update(notification.sender)
        if self.chat_type is None:
            self.info.streams.messages.update(notification.sender)
        notification.center.post_notification('BlinkSessionInfoUpdated', sender=self, data=NotificationData(elements={'media'}))

    def _NH_ChatStreamSMPVerificationDidNotStart(self, notification):
        self._smp_handler.handle_notification(notification)

    def _NH_ChatStreamSMPVerificationDidStart(self, notification):
        self._smp_handler.handle_notification(notification)

    def _NH_ChatStreamSMPVerificationDidEnd(self, notification):
        self._smp_handler.handle_notification(notification)

    def _NH_MessageStreamPGPKeysDidLoad(self, notification):
        self.info.streams.messages.update(notification.sender)

    def _NH_BlinkContactDidChange(self, notification):
        notification.center.post_notification('BlinkSessionContactDidChange', sender=self)


class SMPVerification(Enum):
    Unavailable = 'Unavailable'
    InProgress = 'In progress'
    Succeeded = 'Succeeded'
    Failed = 'Failed'


@implementer(IObserver)
class SMPVerificationHandler(object):

    question = translate('zrtp_widget', 'What is the ZRTP authentication string?').encode('utf-8')

    def __init__(self, blink_session):
        """@type blink_session: BlinkSession"""
        self.blink_session = blink_session
        self.chat_stream = self.blink_session.streams.get('chat')
        if self.chat_stream is None:
            self.chat_stream = self.blink_session.fake_streams.get('messages')
        self.delay = 0 if self.chat_stream.encryption.key_fingerprint > self.chat_stream.encryption.peer_fingerprint else 1
        self.tries = 5

    @property
    def audio_stream(self):
        return self.blink_session.streams.get('audio', Null)

    def start(self):
        call_later(self.delay, self._do_smp)

    def stop(self):
        if self.blink_session.info.streams.chat.smp_status is SMPVerification.InProgress:
            self.blink_session.info.streams.chat.smp_status = SMPVerification.Unavailable
            notification_center = NotificationCenter()
            notification_center.post_notification('BlinkSessionInfoUpdated', sender=self.blink_session, data=NotificationData(elements={'media'}))
        self.chat_stream = Null

    def _do_smp(self):
        if isinstance(self.chat_stream, MessageStream):
            if self.blink_session.info.streams.messages.smp_status in (SMPVerification.InProgress, SMPVerification.Succeeded, SMPVerification.Failed):
                return
            # Set SMP succeeded for message streams until it is implemented in all other clients
            self.blink_session.info.streams.messages.smp_status = SMPVerification.Succeeded
        else:
            if self.blink_session.info.streams.chat.smp_status in (SMPVerification.InProgress, SMPVerification.Succeeded, SMPVerification.Failed):
                return
        audio_stream = self.audio_stream
        if audio_stream.encryption.active and audio_stream.encryption.type == 'ZRTP' and audio_stream.encryption.zrtp.verified:
            self.chat_stream.encryption.smp_verify(audio_stream.encryption.zrtp.sas, question=self.question)

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_ChatStreamSMPVerificationDidNotStart(self, notification):
        if notification.data.reason == 'in progress' and self.blink_session.info.streams.chat.smp_status is not SMPVerification.InProgress:
            # another SMP exchange prevented us from starting, but it was not accepted. reschedule ours as to not lose our attempt.
            call_later(0, self._do_smp)

    def _NH_ChatStreamSMPVerificationDidStart(self, notification):
        if isinstance(self.chat_stream, MessageStream):
            stream_info = self.blink_session.info.streams.messages
        else:
            stream_info = self.blink_session.info.streams.chat

        if notification.data.originator == 'local':
            stream_info.smp_status = SMPVerification.InProgress
            notification.center.post_notification('BlinkSessionInfoUpdated', sender=self.blink_session, data=NotificationData(elements={'media'}))
            return
        if self.blink_session.info.streams.chat.smp_status is SMPVerification.Failed or notification.data.question != self.question:
            self.chat_stream.encryption.smp_abort()
            return
        audio_stream = self.audio_stream
        if audio_stream.encryption.active and audio_stream.encryption.type == 'ZRTP' and audio_stream.encryption.zrtp.sas is not None:
            self.chat_stream.encryption.smp_answer(audio_stream.encryption.zrtp.sas)
            if stream_info.smp_status not in (SMPVerification.Succeeded, SMPVerification.Failed):
                stream_info.smp_status = SMPVerification.InProgress
                notification.center.post_notification('BlinkSessionInfoUpdated', sender=self.blink_session, data=NotificationData(elements={'media'}))
        else:
            self.chat_stream.encryption.smp_abort()

    def _NH_ChatStreamSMPVerificationDidEnd(self, notification):
        if isinstance(self.chat_stream, MessageStream):
            stream_info = self.blink_session.info.streams.messages
        else:
            stream_info = self.blink_session.info.streams.chat

        if stream_info.smp_status in (SMPVerification.Succeeded, SMPVerification.Failed):
            return
        if notification.data.status == SMPStatus.Success:
            if notification.data.same_secrets:
                stream_info.smp_status = SMPVerification.Succeeded if self.blink_session.info.streams.audio.zrtp_verified else SMPVerification.Unavailable
            else:
                stream_info.smp_status = SMPVerification.Failed
        else:
            stream_info.smp_status = SMPVerification.Unavailable
        if notification.data.status is SMPStatus.ProtocolError and notification.data.reason == 'startup collision':
            self.tries -= 1
            self.delay *= 2
            if self.tries > 0:
                call_later(self.delay, self._do_smp)
        elif notification.data.status is SMPStatus.ProtocolError:
            log.warning("SMP exchange got protocol error: {}".format(notification.data.reason))
        notification.center.post_notification('BlinkSessionInfoUpdated', sender=self.blink_session, data=NotificationData(elements={'media'}))

    def _NH_RTPStreamZRTPReceivedSAS(self, notification):
        call_later(self.delay, self._do_smp)

    def _NH_RTPStreamZRTPVerifiedStateChanged(self, notification):
        call_later(self.delay, self._do_smp)


class ClientConference(object):
    def __init__(self):
        self.sessions = []
        self.stream_map = {}
        self.audio_conference = AudioConference()
        self.audio_conference.hold()

    def add_session(self, session):
        audio_stream = session.streams.get('audio')
        self.sessions.append(session)
        self.stream_map[session] = audio_stream
        if audio_stream is not None:
            self.audio_conference.add(audio_stream)

    def remove_session(self, session):
        self.sessions.remove(session)
        audio_stream = self.stream_map.pop(session)
        if audio_stream is not None:
            self.audio_conference.remove(audio_stream)

    def hold(self):
        self.audio_conference.hold()

    def unhold(self):
        self.audio_conference.unhold()


@implementer(IObserver)
class ConferenceParticipant(object):

    def __init__(self, contact, contact_uri):
        self.contact = contact
        self.contact_uri = contact_uri
        self.uri = contact_uri.uri

        self.active_media = set()
        self.display_name = None
        self.on_hold = False
        self.is_composing = False    # TODO: set this from the chat stream -Saul
        self.request_status = None

        notification_center = NotificationCenter()
        notification_center.add_observer(ObserverWeakrefProxy(self), sender=contact)

    def __repr__(self):
        return '%s(%r, %r)' % (self.__class__.__name__, self.contact, self.contact_uri)

    @property
    def pending_request(self):
        return self.request_status is not None

    def _get_is_composing(self):
        return self.__dict__['is_composing']

    def _set_is_composing(self, value):
        old_value = self.__dict__.get('is_composing', False)
        self.__dict__['is_composing'] = value
        if old_value != value:
            NotificationCenter().post_notification('ConferenceParticipantDidChange', sender=self)

    is_composing = property(_get_is_composing, _set_is_composing)
    del _get_is_composing, _set_is_composing

    def _get_request_status(self):
        return self.__dict__['request_status']

    def _set_request_status(self, value):
        old_value = self.__dict__.get('request_status', None)
        self.__dict__['request_status'] = value
        if old_value != value:
            NotificationCenter().post_notification('ConferenceParticipantDidChange', sender=self)

    request_status = property(_get_request_status, _set_request_status)
    del _get_request_status, _set_request_status

    def update(self, data):
        old_values = dict(active_media=self.active_media.copy(), display_name=self.display_name, on_hold=self.on_hold)
        self.display_name = data.display_text.value if data.display_text else None
        self.active_media.clear()
        for media in chain(*data):
            if media.media_type.value == 'message':
                self.active_media.add('chat')
            else:
                self.active_media.add(media.media_type.value)
        audio_endpoints = [endpt for endpt in data if any(media.media_type == 'audio' for media in endpt)]
        self.on_hold = all(endpt.status == 'on-hold' for endpt in audio_endpoints) if audio_endpoints else False
        for attr, value in old_values.items():
            if value != getattr(self, attr):
                NotificationCenter().post_notification('ConferenceParticipantDidChange', sender=self)
                break

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_BlinkContactDidChange(self, notification):
        notification.center.post_notification('ConferenceParticipantDidChange', sender=self)


@implementer(IObserver)
class ServerConference(object):

    sip_prefix_re = re.compile('^sips?:')

    def __init__(self, session):
        self.session = session
        self.sip_session = None

        self.participants = {}
        self.pending_additions = set()
        self.pending_removals = set()

        self.shared_files = []

        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=session)

    def add_participant(self, contact, contact_uri):
        if contact_uri.uri in self.participants:
            return
        participant = ConferenceParticipant(contact, contact_uri)
        participant.request_status = 'Joining'
        self.session.sip_session.conference.add_participant(participant.uri)
        self.participants[participant.uri] = participant
        self.pending_additions.add(participant)
        notification_center = NotificationCenter()
        notification_center.post_notification('BlinkSessionWillAddParticipant', sender=self.session, data=NotificationData(participant=participant))

    def remove_participant(self, participant):
        if participant.uri not in self.participants:
            return
        if participant in self.pending_removals:
            return
        participant.request_status = 'Leaving'
        self.session.sip_session.conference.remove_participant(participant.uri)
        self.pending_removals.add(participant)
        notification_center = NotificationCenter()
        notification_center.post_notification('BlinkSessionWillRemoveParticipant', sender=self.session, data=NotificationData(participant=participant))

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_BlinkSessionDidConnect(self, notification):
        self.sip_session = notification.sender.sip_session
        notification.center.add_observer(self, sender=self.sip_session)

    def _NH_BlinkSessionDidEnd(self, notification):
        if self.sip_session is not None:
            notification.center.remove_observer(self, sender=self.sip_session)
        self.sip_session = None
        self.participants.clear()

    def _NH_BlinkSessionWasDeleted(self, notification):
        notification.center.remove_observer(self, sender=notification.sender)
        self.session = None

    def _NH_SIPSessionGotConferenceInfo(self, notification):
        from blink.contacts import URIUtils
        users = dict((self.sip_prefix_re.sub('', str(user.entity)), user) for user in notification.data.conference_info.users)

        removed_participants = [participant for participant in self.participants.values() if participant.uri not in users and participant not in self.pending_additions]
        confirmed_participants = [participant for participant in self.participants.values() if participant in self.pending_additions and participant.uri in users]
        updated_participants = [self.participants[uri] for uri in users if uri in self.participants]
        added_users = set(users.keys()).difference(list(self.participants.keys()))

        for participant in removed_participants:
            self.participants.pop(participant.uri)
            if participant in self.pending_removals:
                self.pending_removals.remove(participant)
            else:
                notification.center.post_notification('BlinkSessionWillRemoveParticipant', sender=self.session, data=NotificationData(participant=participant))
            notification.center.post_notification('BlinkSessionDidRemoveParticipant', sender=self.session, data=NotificationData(participant=participant))
            participant.request_status = None

        for participant in confirmed_participants:
            participant.request_status = None
            participant.update(users[participant.uri])
            self.pending_additions.remove(participant)
            notification.center.post_notification('BlinkSessionDidAddParticipant', sender=self.session, data=NotificationData(participant=participant))

        for participant in updated_participants:
            participant.update(users[participant.uri])

        for uri in added_users:
            contact, contact_uri = URIUtils.find_contact(uri)
            participant = ConferenceParticipant(contact, contact_uri)
            participant.update(users[participant.uri])
            self.participants[participant.uri] = participant
            notification.center.post_notification('BlinkSessionWillAddParticipant', sender=self.session, data=NotificationData(participant=participant))
            notification.center.post_notification('BlinkSessionDidAddParticipant', sender=self.session, data=NotificationData(participant=participant))

        data = notification.data
        if data.conference_info.conference_description.resources is not None and data.conference_info.conference_description.resources.files is not None:
            files = [file for file in data.conference_info.conference_description.resources.files]
            for file in [file for file in files if (file.hash, file.name) not in self.shared_files]:
                self.shared_files.append((file.hash, file.name))
                sender = SIPURI.parse(file.sender)
                file.contact, contact_uri = URIUtils.find_contact(sender)
                shared_file = File(file.name,
                                   file.size,
                                   file.contact,
                                   file.hash,
                                   str(uuid.uuid4()),
                                   protocol='msrp')

                notification.center.post_notification('BlinkSessionDidShareFile', sender=self.session, data=NotificationData(file=shared_file, direction='incoming'))

    def _NH_SIPConferenceDidNotAddParticipant(self, notification):
        uri = self.sip_prefix_re.sub('', str(notification.data.participant))
        try:
            participant = self.participants[uri]
        except KeyError:
            return
        if participant not in self.pending_additions:
            return
        participant.request_status = None
        del self.participants[uri]
        self.pending_additions.remove(participant)
        notification.center.post_notification('BlinkSessionDidNotAddParticipant', sender=self.session, data=NotificationData(participant=participant, reason=notification.data.reason))

    def _NH_SIPConferenceDidNotRemoveParticipant(self, notification):
        uri = self.sip_prefix_re.sub('', str(notification.data.participant))
        try:
            participant = self.participants[uri]
        except KeyError:
            return
        if participant not in self.pending_removals:
            return
        participant.request_status = None
        self.pending_removals.remove(participant)
        notification.center.post_notification('BlinkSessionDidNotRemoveParticipant', sender=self.session, data=NotificationData(participant=participant, reason=notification.data.reason))

    def _NH_SIPConferenceGotAddParticipantProgress(self, notification):
        uri = self.sip_prefix_re.sub('', str(notification.data.participant))
        try:
            participant = self.participants[uri]
        except KeyError:
            return
        if participant not in self.pending_additions:
            return
        participant.request_status = notification.data.reason

    def _NH_SIPConferenceGotRemoveParticipantProgress(self, notification):
        uri = self.sip_prefix_re.sub('', str(notification.data.participant))
        try:
            participant = self.participants[uri]
        except KeyError:
            return
        if participant not in self.pending_removals:
            return
        participant.request_status = notification.data.reason


# Audio sessions
#

# positions for sessions in a client conference.
class Top(metaclass=MarkerType):    pass
class Middle(metaclass=MarkerType): pass
class Bottom(metaclass=MarkerType): pass


ui_class, base_class = uic.loadUiType(Resources.get('audio_session.ui'))


class AudioSessionWidget(base_class, ui_class):
    def __init__(self, session, parent=None):
        super(AudioSessionWidget, self).__init__(parent)
        with Resources.directory:
            self.setupUi(self)
        # add a left margin for the colored band
        self.address_layout.setContentsMargins(8, -1, -1, -1)
        self.stream_layout.setContentsMargins(8, -1, -1, -1)
        self.bottom_layout.setContentsMargins(8, -1, -1, -1)
        font = self.latency_label.font()
        font.setPointSizeF(self.status_label.fontInfo().pointSizeF() - 1)
        self.latency_label.setFont(font)
        self.packet_loss_label.setFont(font)
        self.duration_label.setMinimumWidth(self.duration_label.fontMetrics().size(Qt.TextFlag.TextSingleLine, '0:00:00').width() + 1)  # prevent the status from shifting left/right when duration changes
        self.info_button.type = LeftSegment
        self.mute_button.type = MiddleSegment
        self.hold_button.type = MiddleSegment
        self.record_button.type = MiddleSegment
        self.hangup_button.type = RightSegment
        self.session = session
        self.selected = False
        self.drop_indicator = False
        self.position_in_conference = None
        self.mute_button.hidden.connect(self._SH_MuteButtonHidden)
        self.mute_button.shown.connect(self._SH_MuteButtonShown)
        self.mute_button.hide()
        self.mute_button.setEnabled(False)
        self.hold_button.setEnabled(False)
        self.record_button.setEnabled(False)
        self.info_button.setEnabled(True)
        self.address_label.setText(session.name)
        self.stream_info_label.session_type = session.type
        self.stream_info_label.codec_info = session.codec_info
        self.duration_label.value = session.duration
        self.latency_label.value = session.latency
        self.packet_loss_label.value = session.packet_loss
        self.status_label.value = session.status
        self.tls_label.setVisible(bool(session.tls))
        self.srtp_label.setVisible(bool(session.srtp))

        self.srtp_label.hovered = False
        self.srtp_label.installEventFilter(self)

        try:
            self.pixmaps
        except AttributeError:
            self.__class__.pixmaps = Container()
            self.pixmaps.blue_lock = QPixmap(Resources.get('icons/lock-blue-12.svg'))
            self.pixmaps.grey_lock = QPixmap(Resources.get('icons/lock-grey-12.svg'))
            self.pixmaps.green_lock = QPixmap(Resources.get('icons/lock-green-12.svg'))
            self.pixmaps.orange_lock = QPixmap(Resources.get('icons/lock-orange-12.svg'))

            def blended_pixmap(pixmap, color):
                blended_pixmap = QPixmap(pixmap)
                painter = QPainter(blended_pixmap)
                painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceAtop)
                painter.fillRect(blended_pixmap.rect(), color)
                painter.end()
                return blended_pixmap

            color = QColor(255, 255, 255, 64)
            self.pixmaps.light_blue_lock = blended_pixmap(self.pixmaps.blue_lock, color)
            self.pixmaps.light_grey_lock = blended_pixmap(self.pixmaps.grey_lock, color)
            self.pixmaps.light_green_lock = blended_pixmap(self.pixmaps.green_lock, color)
            self.pixmaps.light_orange_lock = blended_pixmap(self.pixmaps.orange_lock, color)

    def _get_selected(self):
        return self.__dict__['selected']

    def _set_selected(self, value):
        if self.__dict__.get('selected', None) == value:
            return
        self.__dict__['selected'] = value
        self.update()

    selected = property(_get_selected, _set_selected)
    del _get_selected, _set_selected

    def _get_drop_indicator(self):
        return self.__dict__['drop_indicator']

    def _set_drop_indicator(self, value):
        if self.__dict__.get('drop_indicator', None) == value:
            return
        self.__dict__['drop_indicator'] = value
        self.update()

    drop_indicator = property(_get_drop_indicator, _set_drop_indicator)
    del _get_drop_indicator, _set_drop_indicator

    def _get_position_in_conference(self):
        return self.__dict__['position_in_conference']

    def _set_position_in_conference(self, value):
        if self.__dict__.get('position_in_conference', Null) == value:
            return
        self.__dict__['position_in_conference'] = value
        self.update()

    position_in_conference = property(_get_position_in_conference, _set_position_in_conference)
    del _get_position_in_conference, _set_position_in_conference

    def _SH_MuteButtonHidden(self):
        self.hold_button.type = MiddleSegment

    def _SH_MuteButtonShown(self):
        self.hold_button.type = MiddleSegment

    def _EH_RTPEncryptionLabelClicked(self):
        session = self.session
        stream_info = session.blink_session.info.streams.audio
        if session.audio_stream is not None and not session.audio_stream._done and stream_info.encryption == 'ZRTP':
            rect = QRect(0, 0, 230, 320)
            rect.moveTopRight(self.srtp_label.mapToGlobal(self.srtp_label.rect().bottomRight()))
            rect.translate(0, 3)
            screen_area = QApplication.screenAt(self.srtp_label.pos()).geometry()
            if rect.bottom() > screen_area.bottom():
                rect.moveBottom(self.srtp_label.mapToGlobal(self.srtp_label.rect().topRight()).y() - 3)
            if rect.left() < screen_area.left():
                rect.moveLeft(screen_area.left() + 3)
            session.zrtp_widget.hide()
            session.zrtp_widget.peer_name = stream_info.zrtp_peer_name
            session.zrtp_widget.peer_verified = stream_info.zrtp_verified
            session.zrtp_widget.sas = stream_info.zrtp_sas
            session.zrtp_widget.setGeometry(rect)
            session.zrtp_widget.show()
            session.zrtp_widget.peer_name_value.setFocus(Qt.FocusReason.OtherFocusReason)

    def update_rtp_encryption_icon(self):
        stream = self.session.audio_stream
        stream_info = self.session.blink_session.info.streams.audio
        if self.srtp_label.isEnabled() and stream_info.encryption == 'ZRTP':
            if self.srtp_label.hovered and stream is not None and not stream._done:
                self.srtp_label.setPixmap(self.pixmaps.light_green_lock if stream_info.zrtp_verified else self.pixmaps.light_orange_lock)
            else:
                self.srtp_label.setPixmap(self.pixmaps.green_lock if stream_info.zrtp_verified else self.pixmaps.orange_lock)
        else:
            self.srtp_label.setPixmap(self.pixmaps.grey_lock)

    def eventFilter(self, watched, event):
        event_type = event.type()
        if watched is self.srtp_label:
            if event_type == QEvent.Type.Enter:
                watched.hovered = True
                self.update_rtp_encryption_icon()
            elif event_type == QEvent.Type.Leave:
                watched.hovered = False
                self.update_rtp_encryption_icon()
            elif event_type == QEvent.Type.EnabledChange and not watched.isEnabled():
                watched.setPixmap(self.pixmaps.grey_lock)
            elif event_type in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick) and event.button() == Qt.MouseButton.LeftButton and event.modifiers() == Qt.KeyboardModifier.NoModifier and watched.isEnabled():
                self._EH_RTPEncryptionLabelClicked()
                event.accept()
                return True
        return False

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        rect = self.rect()

        # draw inner rect and border
        #
        if self.selected:
            background = QLinearGradient(0, 0, 10, 0)
            background.setColorAt(0.00, QColor('#75c0ff'))
            background.setColorAt(0.99, QColor('#75c0ff'))
            background.setColorAt(1.00, QColor('#ffffff'))
            painter.setBrush(QBrush(background))
            painter.setPen(QPen(QBrush(QColor('#606060' if self.position_in_conference is None else '#b0b0b0')), 2.0))
        elif self.position_in_conference is not None:
            background = QLinearGradient(0, 0, 10, 0)
            background.setColorAt(0.00, QColor('#95ff95'))
            background.setColorAt(0.99, QColor('#95ff95'))
            background.setColorAt(1.00, QColor('#ffffff'))
            painter.setBrush(QBrush(background))
            painter.setPen(QPen(QBrush(QColor('#b0b0b0')), 2.0))
        else:
            background = QLinearGradient(0, 0, 10, 0)
            background.setColorAt(0.00, QColor('#d0d0d0'))
            background.setColorAt(0.99, QColor('#d0d0d0'))
            background.setColorAt(1.00, QColor('#ffffff'))
            painter.setBrush(QBrush(background))
            painter.setPen(QPen(QBrush(QColor('#b0b0b0')), 2.0))
        painter.drawRoundedRect(rect.adjusted(2, 2, -2, -2), 3, 3)

        # for conferences extend the left marker over the whole conference
        #
        if self.position_in_conference is not None:
            painter.setPen(Qt.PenStyle.NoPen)
            left_rect = rect.adjusted(0, 0, 10-rect.width(), 0)
            if self.position_in_conference is Top:
                painter.drawRect(left_rect.adjusted(2, 5, 0, 5))
            elif self.position_in_conference is Middle:
                painter.drawRect(left_rect.adjusted(2, -5, 0, 5))
            elif self.position_in_conference is Bottom:
                painter.drawRect(left_rect.adjusted(2, -5, 0, -5))

        # draw outer border
        #
        if self.selected or self.drop_indicator:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            if self.drop_indicator:
                painter.setPen(QPen(QBrush(QColor('#dc3169')), 2.0))
            elif self.selected:
                painter.setPen(QPen(QBrush(QColor('#3075c0')), 2.0))  # or #2070c0 (next best look) or gray: #606060

            if self.position_in_conference is Top:
                painter.drawRoundedRect(rect.adjusted(2, 2, -2, 5), 3, 3)
                painter.drawRoundedRect(rect.adjusted(1, 1, -1, 5), 3, 3)
            elif self.position_in_conference is Middle:
                painter.drawRoundedRect(rect.adjusted(2, -5, -2, 5), 3, 3)
                painter.drawRoundedRect(rect.adjusted(1, -5, -1, 5), 3, 3)
            elif self.position_in_conference is Bottom:
                painter.drawRoundedRect(rect.adjusted(2, -5, -2, -2), 3, 3)
                painter.drawRoundedRect(rect.adjusted(1, -5, -1, -1), 3, 3)
            else:
                painter.drawRoundedRect(rect.adjusted(2, 2, -2, -2), 3, 3)
                painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 3, 3)
        elif self.position_in_conference is not None:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QBrush(QColor('#309030')), 2.0))  # or 237523, #2b8f2b
            if self.position_in_conference is Top:
                painter.drawRoundedRect(rect.adjusted(2, 2, -2, 5), 3, 3)
            elif self.position_in_conference is Middle:
                painter.drawRoundedRect(rect.adjusted(2, -5, -2, 5), 3, 3)
            elif self.position_in_conference is Bottom:
                painter.drawRoundedRect(rect.adjusted(2, -5, -2, -2), 3, 3)
            else:
                painter.drawRoundedRect(rect.adjusted(2, 2, -2, -2), 3, 3)

        painter.end()
        super(AudioSessionWidget, self).paintEvent(event)


ui_class, base_class = uic.loadUiType(Resources.get('audio_session_drag.ui'))


class DraggedAudioSessionWidget(base_class, ui_class):
    def __init__(self, session_widget, parent=None):
        super(DraggedAudioSessionWidget, self).__init__(parent)
        with Resources.directory:
            self.setupUi(self)

        self.selected = session_widget.selected
        self.in_conference = session_widget.position_in_conference is not None

        self.address_label.setText(session_widget.address_label.text())
        if self.in_conference:
            self.note_label.setText(translate('sessions', 'Drop outside the conference to detach'))
        else:
            self.note_label.setText(translate('sessions', '<p><b>Drop</b>:&nbsp;Conference&nbsp; <b>Alt+Drop</b>:&nbsp;Transfer</p>'))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if self.in_conference:
            background = QLinearGradient(0, 0, 10, 0)
            background.setColorAt(0.00, QColor('#95ff95'))
            background.setColorAt(0.99, QColor('#95ff95'))
            background.setColorAt(1.00, QColor('#f8f8f8'))
            painter.setBrush(QBrush(background))
            painter.setPen(QPen(QBrush(QColor('#309030')), 2.0))
        elif self.selected:
            background = QLinearGradient(0, 0, 10, 0)
            background.setColorAt(0.00, QColor('#75c0ff'))
            background.setColorAt(0.99, QColor('#75c0ff'))
            background.setColorAt(1.00, QColor('#f8f8f8'))
            painter.setBrush(QBrush(background))
            painter.setPen(QPen(QBrush(QColor('#3075c0')), 2.0))
        else:
            background = QLinearGradient(0, 0, 10, 0)
            background.setColorAt(0.00, QColor('#d0d0d0'))
            background.setColorAt(0.99, QColor('#d0d0d0'))
            background.setColorAt(1.00, QColor('#f8f8f8'))
            painter.setBrush(QBrush(background))
            painter.setPen(QPen(QBrush(QColor('#808080')), 2.0))
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 3, 3)
        painter.end()
        super(DraggedAudioSessionWidget, self).paintEvent(event)


del ui_class, base_class


@implementer(IObserver)
class AudioSessionItem(object):

    def __init__(self, session):
        assert session.items.audio is None
        self.name = session.contact.name
        self.uri = session.uri
        self.blink_session = session
        self.blink_session.items.audio = self

        self.status_context = None
        self.__saved_status = None  # to store skipped status messages during a context change

        self.widget = Null
        self.status = None
        self.type = 'Audio'
        self.codec_info = ''
        self.tls = False
        self.srtp = False
        self.latency = 0
        self.packet_loss = 0
        self.pending_removal = False

        self.zrtp_widget = ZRTPWidget()
        self.zrtp_widget.setWindowFlags(Qt.Popup)
        desktop = QApplication.desktop()
        if hasattr(desktop, 'x11Info') and desktop.x11Info().isCompositingManagerRunning():
            self.zrtp_widget.setAttribute(Qt.WA_TranslucentBackground, True)
        self.zrtp_widget.nameChanged.connect(self._SH_ZRTPWidgetNameChanged)
        self.zrtp_widget.statusChanged.connect(self._SH_ZRTPWidgetStatusChanged)

        self.__deleted__ = False

        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=self.blink_session)
        notification_center.add_observer(self, name='MediaStreamWillEnd')

    def __unicode__(self):
        if self.status is not None:
            return translate('sessions', '{0.type} call with {0.name} ({0.status})').format(self)
        elif self.codec_info:
            return translate('sessions', '{0.type} call with {0.name} using {0.codec_info} ({0.duration!s})').format(self)
        else:
            return translate('sessions', '{0.type} call with {0.name}').format(self)

    @property
    def audio_stream(self):
        return self.blink_session.streams.get('audio')

    def _get_active(self):
        return self.blink_session.active

    def _set_active(self, value):
        self.blink_session.active = bool(value)

    active = property(_get_active, _set_active)
    del _get_active, _set_active

    def _get_client_conference(self):
        return self.blink_session.client_conference

    def _set_client_conference(self, value):
        self.blink_session.client_conference = value

    client_conference = property(_get_client_conference, _set_client_conference)
    del _get_client_conference, _set_client_conference

    def _get_latency(self):
        return self.__dict__['latency']

    def _set_latency(self, value):
        if self.__dict__.get('latency', None) == value:
            return
        self.__dict__['latency'] = value
        self.widget.latency_label.value = value

    latency = property(_get_latency, _set_latency)
    del _get_latency, _set_latency

    def _get_packet_loss(self):
        return self.__dict__['packet_loss']

    def _set_packet_loss(self, value):
        if self.__dict__.get('packet_loss', None) == value:
            return
        self.__dict__['packet_loss'] = value
        self.widget.packet_loss_label.value = value

    packet_loss = property(_get_packet_loss, _set_packet_loss)
    del _get_packet_loss, _set_packet_loss

    def _get_status(self):
        return self.__dict__['status']

    def _set_status(self, value):
        old_status = self.__dict__.get('status', Null)
        new_status = value
        if old_status == new_status:
            return
        if old_status is not None and old_status is not Null:
            context = None if new_status is None else new_status.context
            if self.status_context == old_status.context != context:
                self.__saved_status = value       # preserve the status that is skipped because of context mismatch
                return
            elif old_status.context != context and context is not None:
                self.__saved_status = old_status  # preserve the status that was there prior to switching the context
        self.__dict__['status'] = value
        self.widget.status_label.value = value

    status = property(_get_status, _set_status)
    del _get_status, _set_status

    def _get_type(self):
        return self.__dict__['type']

    def _set_type(self, value):
        if self.__dict__.get('type', Null) == value:
            return
        self.__dict__['type'] = value
        self.widget.stream_info_label.session_type = value

    type = property(_get_type, _set_type)
    del _get_type, _set_type

    def _get_codec_info(self):
        return self.__dict__['codec_info']

    def _set_codec_info(self, value):
        if self.__dict__.get('codec_info', None) == value:
            return
        self.__dict__['codec_info'] = value
        self.widget.stream_info_label.codec_info = value

    codec_info = property(_get_codec_info, _set_codec_info)
    del _get_codec_info, _set_codec_info

    def _get_srtp(self):
        return self.__dict__['srtp']

    def _set_srtp(self, value):
        if self.__dict__.get('srtp', None) == value:
            return
        self.__dict__['srtp'] = value
        self.widget.srtp_label.setVisible(bool(value))

    srtp = property(_get_srtp, _set_srtp)
    del _get_srtp, _set_srtp

    def _get_tls(self):
        return self.__dict__['tls']

    def _set_tls(self, value):
        if self.__dict__.get('tls', None) == value:
            return
        self.__dict__['tls'] = value
        self.widget.tls_label.setVisible(bool(value))

    tls = property(_get_tls, _set_tls)
    del _get_tls, _set_tls

    def _get_widget(self):
        return self.__dict__['widget']

    def _set_widget(self, widget):
        old_widget = self.__dict__.get('widget', Null)
        self.__dict__['widget'] = widget
        if old_widget is not Null:
            old_widget.mute_button.clicked.disconnect(self._SH_MuteButtonClicked)
            old_widget.hold_button.clicked.disconnect(self._SH_HoldButtonClicked)
            old_widget.record_button.clicked.disconnect(self._SH_RecordButtonClicked)
            old_widget.info_button.clicked.disconnect(self._SH_InfoButtonClicked)
            old_widget.hangup_button.clicked.disconnect(self._SH_HangupButtonClicked)
            widget.mute_button.setEnabled(old_widget.mute_button.isEnabled())
            widget.mute_button.setChecked(old_widget.mute_button.isChecked())
            widget.hold_button.setEnabled(old_widget.hold_button.isEnabled())
            widget.hold_button.setChecked(old_widget.hold_button.isChecked())
            widget.record_button.setEnabled(old_widget.record_button.isEnabled())
            widget.record_button.setChecked(old_widget.record_button.isChecked())
            widget.hangup_button.setEnabled(old_widget.hangup_button.isEnabled())
        widget.mute_button.clicked.connect(self._SH_MuteButtonClicked)
        widget.hold_button.clicked.connect(self._SH_HoldButtonClicked)
        widget.info_button.clicked.connect(self._SH_InfoButtonClicked)
        widget.record_button.clicked.connect(self._SH_RecordButtonClicked)
        widget.hangup_button.clicked.connect(self._SH_HangupButtonClicked)

    widget = property(_get_widget, _set_widget)
    del _get_widget, _set_widget

    @property
    def duration(self):
        return self.blink_session.info.duration

    def end(self):
        if self.audio_stream in self.blink_session.streams.proposed and self.blink_session.state == 'connected/sent_proposal':
            self.blink_session.sip_session.cancel_proposal()
        # review this -Dan
        # elif len(self.blink_session.streams) > 1 and self.blink_session.state == 'connected':
        #    self.blink_session.remove_stream(self.audio_stream)
        elif 'chat' in self.blink_session.streams and self.blink_session.state == 'connected':
            self.blink_session.remove_streams([stream for stream in self.blink_session.streams if stream.type != 'chat'])
        else:
            self.blink_session.end()

    def delete(self):
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=self.blink_session)
        notification_center.remove_observer(self, name='MediaStreamWillEnd')
        self.blink_session.items.audio = None
        self.blink_session = None
        self.zrtp_widget = None
        self.widget = Null

    def send_dtmf(self, digit):
        self.blink_session.send_dtmf(digit)

    def _cleanup(self):
        if self.__deleted__:
            return
        self.__deleted__ = True
        self.widget.mute_button.setEnabled(False)
        self.widget.hold_button.setEnabled(False)
        self.widget.record_button.setEnabled(False)
        self.widget.hangup_button.setEnabled(False)

    def _reset_status(self, expected_status):
        if self.status == expected_status:
            self.status = self.__saved_status
        self.__saved_status = None

    def _SH_HangupButtonClicked(self):
        self.end()

    def _SH_HoldButtonClicked(self, checked):
        if checked:
            self.blink_session.hold()
        else:
            self.blink_session.unhold()

    def _SH_InfoButtonClicked(self, checked):
        blink = QApplication.instance()
        blink.chat_window.show_session_info()

    def _SH_MuteButtonClicked(self, checked):
        if self.audio_stream is not None:
            self.audio_stream.muted = checked

    def _SH_RecordButtonClicked(self, checked):
        if checked:
            self.blink_session.start_recording()
        else:
            self.blink_session.stop_recording()

    def _SH_ZRTPWidgetNameChanged(self):
        stream = self.blink_session.streams.get('audio', Null)
        stream.encryption.zrtp.peer_name = self.zrtp_widget.peer_name

    def _SH_ZRTPWidgetStatusChanged(self):
        stream = self.blink_session.streams.get('audio', Null)
        stream.encryption.zrtp.verified = self.zrtp_widget.peer_verified

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_BlinkSessionConnectionProgress(self, notification):
        stage = notification.data.stage
        if stage == 'initializing':
            self.status = Status(translate('sessions', 'Initializing...'))
        elif stage == 'connecting/dns_lookup':
            self.status = Status(translate('sessions', 'Looking up destination...'))
        elif stage == 'connecting' and self.blink_session.routes:
            self.tls = self.blink_session.transport == 'tls'
            uri = self.blink_session.routes[0].uri
            destination = '%s:%s' % (self.blink_session.transport, uri.host.decode())
            self.status = Status(translate('sessions', 'Trying %s') % destination)
        elif stage == 'ringing':
            self.status = Status(translate('sessions', 'Ringing...'))
        elif stage == 'starting':
            self.status = Status(translate('sessions', 'Starting media...'))
        else:
            self.status = None

    def _NH_BlinkSessionInfoUpdated(self, notification):
        if 'media' in notification.data.elements:
            audio_info = self.blink_session.info.streams.audio
            self.type = translate('sessions', 'HD Audio') if audio_info.sample_rate and audio_info.sample_rate >= 16000 else translate('sessions', 'Audio')
            self.codec_info = audio_info.codec
            if audio_info.encryption is not None:
                self.widget.srtp_label.setToolTip(translate('sessions', 'Media is encrypted using %s (%s)') % (audio_info.encryption, audio_info.encryption_cipher))
            else:
                self.widget.srtp_label.setToolTip(translate('sessions', 'Media is not encrypted'))
            self.widget.update_rtp_encryption_icon()
            self.srtp = audio_info.encryption is not None
        if 'statistics' in notification.data.elements:
            self.widget.duration_label.value = self.blink_session.info.duration
            # TODO: compute packet loss and latency statistics -Saul

    def _NH_BlinkSessionDidChangeHoldState(self, notification):
        self.widget.hold_button.setChecked(notification.data.local_hold)
        if self.blink_session.state == 'connected':
            if notification.data.local_hold:
                self.status = Status(translate('sessions', 'On hold'), color='#000090')
            elif notification.data.remote_hold:
                self.status = Status(translate('sessions', 'Hold by remote'), color='#000090')
            else:
                self.status = None

    def _NH_BlinkSessionDidChangeRecordingState(self, notification):
        self.widget.record_button.setChecked(notification.data.recording)

    def _NH_BlinkSessionDidConnect(self, notification):
        session = notification.sender
        self.tls = session.transport == 'tls'
        if 'audio' in session.streams:
            self.widget.mute_button.setEnabled(True)
            self.widget.hold_button.setEnabled(True)
            self.widget.record_button.setEnabled(True)
            self.widget.hangup_button.setEnabled(True)
            self.status = Status('Connected')
            settings = SIPSimpleSettings()
            if settings.sip.auto_record:
                self.blink_session.start_recording()
            call_later(3, self._reset_status, self.status)  # reset status 3 seconds later if it hasn't changed until then
        else:
            self.status = Status(translate('sessions', 'Audio refused'), color='#900000')
            self._cleanup()

    def _NH_BlinkSessionDidAddStream(self, notification):
        if notification.data.stream.type == 'audio':
            self.widget.mute_button.setEnabled(True)
            self.widget.hold_button.setEnabled(True)
            self.widget.record_button.setEnabled(True)
            self.widget.hangup_button.setEnabled(True)
            self.status = Status('Connected')
            call_later(3, self._reset_status, self.status)  # reset status 3 seconds later if it hasn't changed until then

    def _NH_BlinkSessionDidNotAddStream(self, notification):
        if notification.data.stream.type == 'audio':
            self.status = Status(translate('sessions', 'Audio refused'), color='#900000')  # where can we get the reason from? (rejected, cancelled, failed, ...) -Dan
            self._cleanup()

    def _NH_BlinkSessionWillRemoveStream(self, notification):
        if notification.data.stream.type == 'audio':
            self.status = Status('Ending...')
            self.widget.mute_button.setEnabled(False)
            self.widget.hold_button.setEnabled(False)
            self.widget.record_button.setEnabled(False)
            self.widget.hangup_button.setEnabled(False)

    def _NH_BlinkSessionDidRemoveStream(self, notification):
        if notification.data.stream.type == 'audio':
            self.status = Status('Audio removed') if self.blink_session.streams else Status('Call ended')
            self._cleanup()

    def _NH_BlinkSessionWillEnd(self, notification):
        self.status = Status('Ending...')
        self.widget.mute_button.setEnabled(False)
        self.widget.hold_button.setEnabled(False)
        self.widget.record_button.setEnabled(False)
        self.widget.hangup_button.setEnabled(False)

    def _NH_BlinkSessionDidEnd(self, notification):
        if not self.__deleted__:  # may have been removed by BlinkSessionDidRemoveStream less than 5 seconds before the session ended.
            if notification.data.error:
                self.status = Status(notification.data.reason, color='#900000')
            else:
                self.status = Status(notification.data.reason)
            self._cleanup()

    def _NH_BlinkSessionTransferNewOutgoing(self, notification):
        self.status_context = 'transfer'
        self.status = Status(translate('sessions', 'Transfer: Trying'), context='transfer')

    def _NH_BlinkSessionTransferDidEnd(self, notification):
        if self.blink_session.transfer_direction == 'outgoing':
            self.status = Status(translate('sessions', 'Transfer: Succeeded'), context='transfer')

    def _NH_BlinkSessionTransferDidFail(self, notification):
        if self.blink_session.transfer_direction == 'outgoing':
            # TODO: maybe translate? -Tijmen
            reason_map = {403: 'Forbidden', 404: 'Not Found', 408: 'Timeout', 480: 'Unavailable', 486: 'Busy',
                          487: 'Cancelled', 488: 'Not Acceptable', 600: 'Busy', 603: 'Declined'}
            reason = reason_map.get(notification.data.code, translate('sessions', 'Failed'))
            self.status = Status(translate('sessions', "Transfer: {}").format(reason), context='transfer')
            call_later(3, self._reset_status, self.status)
            self.status_context = None

    def _NH_BlinkSessionTransferGotProgress(self, notification):
        if notification.data.code < 200:  # final answers are handled in DidEnd and DiDFail
            self.status = Status(translate('sessions', "Transfer: {}").format(notification.data.reason), context='transfer')

    def _NH_MediaStreamWillEnd(self, notification):
        stream = notification.sender
        if stream.type == 'audio' and stream.blink_session.items.audio is self:
            self.zrtp_widget.hide()


class AudioSessionDelegate(QStyledItemDelegate):
    size_hint = QSize(220, 76)

    def __init__(self, parent=None):
        super(AudioSessionDelegate, self).__init__(parent)

    def createEditor(self, parent, options, index):
        session = index.data(Qt.ItemDataRole.UserRole)
        session.widget = AudioSessionWidget(session, parent)
        session.widget.hold_button.clicked.connect(self._SH_HoldButtonClicked)
        return session.widget

    def updateEditorGeometry(self, editor, option, index):
        editor.setGeometry(option.rect)

    def paint(self, painter, option, index):
        session = index.data(Qt.ItemDataRole.UserRole)
        if session.widget.size() != option.rect.size():
            # For some reason updateEditorGeometry only receives the peak value
            # of the size that the widget ever had, so it will never shrink it.
            session.widget.resize(option.rect.size())

    def sizeHint(self, option, index):
        return self.size_hint

    def _SH_HoldButtonClicked(self, checked):
        session_widget = self.sender().parent()
        session = session_widget.session
        if session.client_conference is None and not session.active and not checked:
            session_list = self.parent()
            model = session_list.model()
            selection_model = session_list.selectionModel()
            selection_model.select(model.index(model.sessions.index(session)), selection_model.SelectionFlag.ClearAndSelect)


@implementer(IObserver)
class AudioSessionModel(QAbstractListModel):

    sessionAboutToBeAdded = pyqtSignal(AudioSessionItem)
    sessionAboutToBeRemoved = pyqtSignal(AudioSessionItem)
    sessionAdded = pyqtSignal(AudioSessionItem)
    sessionRemoved = pyqtSignal(AudioSessionItem)
    structureChanged = pyqtSignal()

    # The MIME types we accept in drop operations, in the order they should be handled
    accepted_mime_types = ['application/x-blink-session-list', 'application/x-blink-contact-list', 'application/x-blink-contact-uri-list']

    def __init__(self, parent=None):
        super(AudioSessionModel, self).__init__(parent)
        self.sessions = []
        self.session_list = parent.session_list

        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='BlinkSessionNewIncoming')
        notification_center.add_observer(self, name='BlinkSessionWillReinitialize')
        notification_center.add_observer(self, name='BlinkSessionDidReinitializeForIncoming')
        notification_center.add_observer(self, name='BlinkSessionWillConnect')
        notification_center.add_observer(self, name='BlinkSessionDidConnect')
        notification_center.add_observer(self, name='BlinkSessionWillAddStream')
        notification_center.add_observer(self, name='BlinkSessionDidNotAddStream')
        notification_center.add_observer(self, name='BlinkSessionDidRemoveStream')
        notification_center.add_observer(self, name='BlinkSessionDidEnd')
        notification_center.add_observer(self, name='BlinkSessionDidChangeClientConference')

    @property
    def active_sessions(self):
        return [session for session in self.sessions if not session.pending_removal]

    def flags(self, index):
        if index.isValid():
            return QAbstractListModel.flags(self, index) | Qt.ItemFlag.ItemIsDropEnabled | Qt.ItemFlag.ItemIsDragEnabled | Qt.ItemFlag.ItemIsEditable
        else:
            return QAbstractListModel.flags(self, index)

    def rowCount(self, parent=QModelIndex()):
        return len(self.sessions)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        item = self.sessions[index.row()]
        if role == Qt.ItemDataRole.UserRole:
            return item
        elif role == Qt.ItemDataRole.DisplayRole:
            return str(item)
        return None

    def supportedDropActions(self):
        return Qt.DropAction.CopyAction | Qt.DropAction.MoveAction | Qt.DropAction.LinkAction

    def mimeTypes(self):
        return ['application/x-blink-session-list']

    def mimeData(self, indexes):
        mime_data = QMimeData()
        sessions = [self.sessions[index.row()] for index in indexes if index.isValid()]
        if sessions:
            # TODO: pass a session id which can then be fetched from the SessionManager -Saul
            mime_data.setData('application/x-blink-session-list', QByteArray())
        return mime_data

    def dropMimeData(self, mime_data, action, row, column, parent_index):
        # this is here just to keep the default Qt DnD API happy
        # the custom handler is in handleDroppedData
        return False

    def handleDroppedData(self, mime_data, action, index):
        if action == Qt.DropAction.IgnoreAction:
            return True

        for mime_type in self.accepted_mime_types:
            if mime_data.hasFormat(mime_type):
                name = mime_type.replace('/', ' ').replace('-', ' ').title().replace(' ', '')
                handler = getattr(self, '_DH_%s' % name)
                return handler(mime_data, action, index)
        else:
            return False

    def _DH_ApplicationXBlinkSessionList(self, mime_data, action, index):
        session_list = self.session_list
        selection_model = session_list.selectionModel()
        source = session_list.dragged_session
        target = self.sessions[index.row()] if index.isValid() else None

        if action == Qt.DropAction.LinkAction:  # call transfer
            source.blink_session.transfer(target.blink_session.contact_uri, replaced_session=target.blink_session)
        elif source.client_conference is None:  # the dragged session is not in a conference yet
            if target.client_conference is not None:
                source_row = self.sessions.index(source)
                target_row = self.sessions.index(target.client_conference.sessions[-1].items.audio) + 1
                if self.beginMoveRows(QModelIndex(), source_row, source_row, QModelIndex(), target_row):
                    insert_point = target_row if source_row >= target_row else target_row-1
                    self.sessions.remove(source)
                    self.sessions.insert(insert_point, source)
                    self.endMoveRows()
                source.client_conference = target.client_conference
                session_list.scrollTo(self.index(self.sessions.index(source)), session_list.ScrollHint.EnsureVisible)  # is this even needed? -Dan
            else:
                target_row = self.sessions.index(target)
                if self.beginMoveRows(QModelIndex(), target_row, target_row, QModelIndex(), 0):
                    self.sessions.remove(target)
                    self.sessions.insert(0, target)
                    self.endMoveRows()
                source_row = self.sessions.index(source)
                if self.beginMoveRows(QModelIndex(), source_row, source_row, QModelIndex(), 1):
                    self.sessions.remove(source)
                    self.sessions.insert(1, source)
                    self.endMoveRows()
                conference = ClientConference()
                target.client_conference = conference  # must add them to the conference in the same order they are in the list (target is first, source is last)
                source.client_conference = conference
                session_list.scrollToTop()
            for session in source.client_conference.sessions:
                session.items.audio.widget.selected = source.widget.selected or target.widget.selected
                session.active = source.active or target.active
            if source.active:
                source.client_conference.unhold()
            self.structureChanged.emit()
        else:  # the dragged session is in a conference
            dragged = source
            sibling = next(session.items.audio for session in dragged.client_conference.sessions if session.items.audio is not dragged)
            if selection_model.isSelected(self.index(self.sessions.index(dragged))):
                selection_model.select(self.index(self.sessions.index(sibling)), selection_model.SelectionFlag.ClearAndSelect)
            if len(dragged.client_conference.sessions) == 2:
                dragged.client_conference = None
                sibling.client_conference = None
                # # maybe only move past the last conference to minimize movement. see how this feels during usage. (or sort them alphabetically with conferences at the top) -Dan
                # for position, session in enumerate(self.sessions):
                #    if session not in (dragged, sibling) and session.client_conference is None:
                #        move_point = position
                #        break
                # else:
                #    move_point = len(self.sessions)
                move_point = len(self.sessions)
                dragged_row = self.sessions.index(dragged)
                if self.beginMoveRows(QModelIndex(), dragged_row, dragged_row, QModelIndex(), move_point):
                    self.sessions.remove(dragged)
                    self.sessions.insert(move_point-1, dragged)
                    self.endMoveRows()
                move_point -= 1
                sibling_row = self.sessions.index(sibling)
                if self.beginMoveRows(QModelIndex(), sibling_row, sibling_row, QModelIndex(), move_point):
                    self.sessions.remove(sibling)
                    self.sessions.insert(move_point-1, sibling)
                    self.endMoveRows()
                session_list.scrollToBottom()
            else:
                dragged.client_conference = None
                move_point = len(self.sessions)
                dragged_row = self.sessions.index(dragged)
                if self.beginMoveRows(QModelIndex(), dragged_row, dragged_row, QModelIndex(), move_point):
                    self.sessions.remove(dragged)
                    self.sessions.append(dragged)
                    self.endMoveRows()
                session_list.scrollTo(self.index(self.sessions.index(sibling)), session_list.PositionAtCenter)
            dragged.widget.selected = False
            dragged.active = False
            self.structureChanged.emit()
        return True

    def _DH_ApplicationXBlinkContactList(self, mime_data, action, index):
        if not index.isValid():
            return
        try:
            contacts = pickle.loads(str(mime_data.data('application/x-blink-contact-list')))
        except Exception:
            return False
        session = self.sessions[index.row()]
        session_manager = SessionManager()
        for contact in contacts:
            session_manager.create_session(contact, contact.uri, [StreamDescription('audio')], sibling=session.blink_session)
        return True

    def _DH_ApplicationXBlinkContactUriList(self, mime_data, action, index):
        if not index.isValid():
            return
        try:
            contact, contact_uris = pickle.loads(str(mime_data.data('application/x-blink-contact-uri-list')))
        except Exception:
            return False
        session = self.sessions[index.row()]
        session_manager = SessionManager()
        for contact_uri in contact_uris:
            session_manager.create_session(contact, contact_uri.uri, [StreamDescription('audio')], sibling=session.blink_session)
        return True

    def _add_session(self, session):
        position = len(self.sessions)
        self.beginInsertRows(QModelIndex(), position, position)
        self.sessions.append(session)
        self.endInsertRows()
        self.session_list.openPersistentEditor(self.index(position))

    def _remove_session(self, session):
        position = self.sessions.index(session)
        self.beginRemoveRows(QModelIndex(), position, position)
        del self.sessions[position]
        self.endRemoveRows()

    def addSession(self, session):
        if session in self.sessions:
            return
        self.sessionAboutToBeAdded.emit(session)
        self._add_session(session)
        # not the right place to do this. the list should do it (else the model needs a back-reference to the list), however in addSessionAndConference we can't avoid doing it -Dan
        selection_model = self.session_list.selectionModel()
        selection_model.select(self.index(self.rowCount()-1), selection_model.SelectionFlag.ClearAndSelect)
        self.sessionAdded.emit(session)
        self.structureChanged.emit()

    def addSessionAndConference(self, session, sibling):
        if session in self.sessions:
            return
        if sibling not in self.sessions:
            raise ValueError('sibling %r not in sessions list' % sibling)
        self.sessionAboutToBeAdded.emit(session)
        session_list = self.session_list
        if sibling.client_conference is not None:
            position = self.sessions.index(sibling.client_conference.sessions[-1].items.audio) + 1
            self.beginInsertRows(QModelIndex(), position, position)
            self.sessions.insert(position, session)
            self.endInsertRows()
            session_list.openPersistentEditor(self.index(position))
            session.client_conference = sibling.client_conference
            session_list.scrollTo(self.index(position), session_list.EnsureVisible)  # or PositionAtBottom (is this even needed? -Dan)
        else:
            sibling_row = self.sessions.index(sibling)
            if self.beginMoveRows(QModelIndex(), sibling_row, sibling_row, QModelIndex(), 0):
                self.sessions.remove(sibling)
                self.sessions.insert(0, sibling)
                self.endMoveRows()
            self.beginInsertRows(QModelIndex(), 1, 1)
            self.sessions.insert(1, session)
            self.endInsertRows()
            session_list.openPersistentEditor(self.index(1))
            conference = ClientConference()
            sibling.client_conference = conference  # must add them to the conference in the same order they are in the list (sibling first, new session last)
            session.client_conference = conference
            if sibling.active:
                conference.unhold()
            session_list.scrollToTop()
        session.widget.selected = sibling.widget.selected
        session.active = sibling.active
        self.sessionAdded.emit(session)
        self.structureChanged.emit()

    def removeSession(self, session):
        if session not in self.sessions:
            return
        self.sessionAboutToBeRemoved.emit(session)
        session_list = self.session_list
        selection_mode = session_list.selectionMode()
        session_list.setSelectionMode(session_list.SelectionMode.NoSelection)
        if session.client_conference is not None:
            sibling = next(s.items.audio for s in session.client_conference.sessions if s.items.audio is not session)
            session_index = self.index(self.sessions.index(session))
            sibling_index = self.index(self.sessions.index(sibling))
            selection_model = session_list.selectionModel()
            if selection_model.isSelected(session_index):
                selection_model.select(sibling_index, selection_model.SelectionFlag.ClearAndSelect)
        self._remove_session(session)
        session_list.setSelectionMode(selection_mode)
        if session.client_conference is not None:
            if len(session.client_conference.sessions) == 2:
                first, last = session.client_conference.sessions
                first.client_conference = None
                last.client_conference = None
            else:
                session.client_conference = None

        session.delete()

        self.sessionRemoved.emit(session)
        self.structureChanged.emit()

    def conferenceSessions(self, sessions):
        session_list = self.session_list
        selected = any(session.widget.selected for session in sessions)
        active = any(session.active for session in sessions)
        conference = ClientConference()
        for position, session in enumerate(sessions):
            session_row = self.sessions.index(session)
            if self.beginMoveRows(QModelIndex(), session_row, session_row, QModelIndex(), position):
                self.sessions.remove(session)
                self.sessions.insert(position, session)
                self.endMoveRows()
            session.client_conference = conference
            session.widget.selected = selected
            session.active = active
        if active:
            conference.unhold()
        session_list.scrollToTop()
        self.structureChanged.emit()

    def breakConference(self, conference):  # replace this by an endConference (or terminate/hangupConference) functionality -Dan
        sessions = [blink_session.items.audio for blink_session in conference.sessions]
        session_list = self.session_list
        selection_model = session_list.selectionModel()
        selection = selection_model.selection()
        selected_session = selection[0].topLeft().data(Qt.ItemDataRole.UserRole) if selection else None
        move_point = len(self.sessions)
        for index, session in enumerate(reversed(sessions)):
            session_row = self.sessions.index(session)
            if self.beginMoveRows(QModelIndex(), session_row, session_row, QModelIndex(), move_point-index):
                self.sessions.remove(session)
                self.sessions.insert(move_point-index-1, session)
                self.endMoveRows()
            session.client_conference = None
            session.widget.selected = session is selected_session
            session.active = session is selected_session
        session_list.scrollToBottom()
        self.structureChanged.emit()

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_BlinkSessionNewIncoming(self, notification):
        session = notification.sender
        if 'audio' in session.streams:
            session_item = AudioSessionItem(session)
            self.addSession(session_item)

    def _NH_BlinkSessionDidReinitializeForIncoming(self, notification):
        session = notification.sender
        if 'audio' in session.streams:
            session_item = AudioSessionItem(session)
            self.addSession(session_item)

    def _NH_BlinkSessionWillConnect(self, notification):
        session = notification.sender
        if 'audio' in session.streams:
            session_item = AudioSessionItem(session)
            if notification.data.sibling is not None:
                self.addSessionAndConference(session_item, notification.data.sibling.items.audio)
            else:
                self.addSession(session_item)

    def _NH_BlinkSessionDidConnect(self, notification):
        session = notification.sender
        session_item = session.items.audio
        if session_item is not None and 'audio' not in session.streams:
            session_item.pending_removal = True
            call_later(5, self.removeSession, session_item)
            self.structureChanged.emit()

    def _NH_BlinkSessionWillAddStream(self, notification):
        if notification.data.stream.type == 'audio':
            if notification.sender.items.audio is not None:
                self.removeSession(notification.sender.items.audio)
            session_item = AudioSessionItem(notification.sender)
            self.addSession(session_item)

    def _NH_BlinkSessionDidNotAddStream(self, notification):
        if notification.data.stream.type == 'audio' and notification.sender.items.audio:
            session_item = notification.sender.items.audio
            session_item.pending_removal = True
            call_later(5, self.removeSession, session_item)
            self.structureChanged.emit()

    def _NH_BlinkSessionDidRemoveStream(self, notification):
        if notification.data.stream.type == 'audio' and notification.sender.items.audio:
            session_item = notification.sender.items.audio
            session_item.pending_removal = True
            call_later(5, self.removeSession, session_item)
            self.structureChanged.emit()

    def _NH_BlinkSessionDidEnd(self, notification):
        session_item = notification.sender.items.audio
        if session_item is not None and not session_item.pending_removal:
            session_item.pending_removal = True
            call_later(5, self.removeSession, session_item)
            self.structureChanged.emit()

    def _NH_BlinkSessionWillReinitialize(self, notification):
        session_item = notification.sender.items.audio
        if session_item is not None:
            self.removeSession(session_item)

    def _NH_BlinkSessionDidChangeClientConference(self, notification):  # would this better be handled by the audio session item itself? (apparently not) -Dan
        session = notification.sender.items.audio

        if not notification.data.new_conference:
            session.widget.position_in_conference = None
            session.widget.mute_button.hide()
        if session.widget.mute_button.isChecked():
            session.widget.mute_button.click()

        for conference in (conference for conference in (notification.data.old_conference, notification.data.new_conference) if conference):
            session_count = len(conference.sessions)
            if session_count == 1:
                blink_session = conference.sessions[0]
                session = blink_session.items.audio
                session.widget.position_in_conference = None
                session.widget.mute_button.hide()
            elif session_count > 1:
                for blink_session in conference.sessions:
                    session = blink_session.items.audio
                    session.widget.position_in_conference = Top if blink_session is conference.sessions[0] else Bottom if blink_session is conference.sessions[-1] else Middle
                    session.widget.mute_button.show()


@implementer(IObserver)
class AudioSessionListView(QListView):

    def __init__(self, parent=None):
        super(AudioSessionListView, self).__init__(parent)
        self.setItemDelegate(AudioSessionDelegate(self))
        self.setDropIndicatorShown(False)
        self.context_menu = QMenu(self)
        self.actions = ContextMenuActions()
        self.actions.send_messages = QAction(translate('audio_session', "Send Messages"), self, triggered=self._AH_SendSMS)
        self.actions.send_files = QAction(translate('audio_session', "Send File(s)..."), self, triggered=self._AH_SendFiles)
        self.actions.add_audio = QAction(translate('audio_session', "Add audio"), self, triggered=self._AH_AddAudio)
        self.actions.remove_audio = QAction(translate('audio_session', "Remove audio"), self, triggered=self._AH_RemoveAudio)
        self.actions.add_video = QAction(translate('audio_session', "Add video"), self, triggered=self._AH_AddVideo)
        self.actions.remove_video = QAction(translate('audio_session', "Remove video"), self, triggered=self._AH_RemoveVideo)
        self.actions.share_my_screen = QAction(translate('audio_session', "Share my screen"), self, triggered=self._AH_ShareMyScreen)
        self.actions.request_screen = QAction(translate('audio_session', "Request screen"), self, triggered=self._AH_RequestScreen)
        self.actions.end_screen_sharing = QAction(translate('audio_session', "End screen sharing"), self, triggered=self._AH_EndScreenSharing)
        self.actions.add_chat = QAction(translate('audio_session', "Add MSRP chat"), self, triggered=self._AH_AddChat)
        self.actions.remove_chat = QAction(translate('audio_session', "Remove MSRP chat"), self, triggered=self._AH_RemoveChat)
        self.actions.session_info = QAction(translate('audio_session', "Show session info"), self, triggered=self._AH_ShowSessionInfo)
        self.dragged_session = None
        self.ignore_selection_changes = False
        self._pressed_position = None
        self._pressed_index = None
        self._hangup_shortcuts = []
        self._hangup_shortcuts.append(QShortcut('Ctrl+Esc', self, member=self._SH_HangupShortcutActivated, context=Qt.ShortcutContext.ApplicationShortcut))
        self._hangup_shortcuts.append(QShortcut('Ctrl+Delete', self, member=self._SH_HangupShortcutActivated, context=Qt.ShortcutContext.ApplicationShortcut))
        self._hangup_shortcuts.append(QShortcut('Ctrl+Backspace', self, member=self._SH_HangupShortcutActivated, context=Qt.ShortcutContext.ApplicationShortcut))
        self._hold_shortcut = QShortcut('Ctrl+Space', self, member=self._SH_HoldShortcutActivated, context=Qt.ShortcutContext.ApplicationShortcut)
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='BlinkActiveSessionDidChange')

    def _update_control_menu(self, item):
        menu = self.context_menu
        menu.hide()
        blink_session = item.blink_session
        state = blink_session.state
        if state == 'connected':
            stream_types = blink_session.streams.types
            if 'audio' not in stream_types:
                menu.addAction(self.actions.add_audio)
            if stream_types != {'audio'} and not stream_types.intersection({'screen-sharing', 'video'}):
                menu.addAction(self.actions.remove_audio)
            if 'video' not in stream_types:
                menu.addAction(self.actions.add_video)
            elif stream_types != {'video'}:
                menu.addAction(self.actions.remove_video)
            if 'chat' not in stream_types:
                menu.addAction(self.actions.send_messages)
                menu.addAction(self.actions.add_chat)
            elif stream_types != {'chat'}:
                menu.addAction(self.actions.remove_chat)
            if 'screen-sharing' not in stream_types:
                menu.addAction(self.actions.request_screen)
                menu.addAction(self.actions.share_my_screen)
            elif stream_types != {'screen-sharing'}:
                menu.addAction(self.actions.end_screen_sharing)
            menu.addAction(self.actions.send_files)
            menu.addSeparator()
        menu.addAction(self.actions.session_info)

    def contextMenuEvent(self, event):
        menu = self.context_menu
        index = self.indexAt(event.pos())
        if index.isValid():
            item = index.data(Qt.ItemDataRole.UserRole)
            blink_session = item.blink_session
            state = blink_session.state
            if state == 'connected/received_proposal':
                return
            menu.clear()
            self._update_control_menu(item)
        menu.exec(self.mapToGlobal(event.pos()))

    def hideEvent(self, event):
        self.context_menu.hide()

    def keyPressEvent(self, event):
        char = event.text().upper()
        if char and char in string.digits + string.ascii_uppercase + '#*':
            digit_map  = {'2': 'ABC', '3': 'DEF', '4': 'GHI', '5': 'JKL', '6': 'MNO', '7': 'PQRS', '8': 'TUV', '9': 'WXYZ'}
            letter_map = {letter: digit for digit, letter_group in digit_map.items() for letter in letter_group}
            for session in (s for s in self.model().sessions if s.active):
                session.send_dtmf(letter_map.get(char, char))
        elif event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            selection_model = self.selectionModel()
            current_index = selection_model.currentIndex()
            if current_index.isValid():
                step = 1 if event.key() == Qt.Key.Key_Down else -1
                conference = current_index.data(Qt.ItemDataRole.UserRole).client_conference
                new_index = current_index.sibling(current_index.row()+step, current_index.column())
                while conference is not None and new_index.isValid() and new_index.data(Qt.ItemDataRole.UserRole).client_conference is conference:
                    new_index = new_index.sibling(new_index.row()+step, new_index.column())
                if new_index.isValid():
                    selection_model.select(new_index, selection_model.SelectionFlag.ClearAndSelect)
        else:
            super(AudioSessionListView, self).keyPressEvent(event)

    def mousePressEvent(self, event):
        self._pressed_position = event.pos()
        self._pressed_index = self.indexAt(self._pressed_position)
        super(AudioSessionListView, self).mousePressEvent(event)
        selection_model = self.selectionModel()
        selected_indexes = selection_model.selectedIndexes()
        if selected_indexes:
            selection_model.setCurrentIndex(selected_indexes[0], selection_model.SelectionFlag.Select)
        else:
            selection_model.setCurrentIndex(self.model().index(-1), selection_model.SelectionFlag.Select)

    def mouseReleaseEvent(self, event):
        self._pressed_position = None
        self._pressed_index = None
        super(AudioSessionListView, self).mouseReleaseEvent(event)

    def selectionCommand(self, index, event=None):
        selection_model = self.selectionModel()
        if self.selectionMode() == self.SelectionMode.NoSelection:
            return selection_model.SelectionFlag.NoUpdate
        elif not index.isValid() or event is None:
            return selection_model.SelectionFlag.NoUpdate
        elif event.type() == QEvent.Type.MouseButtonPress and not selection_model.selection():
            return selection_model.SelectionFlag.ClearAndSelect
        elif event.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseMove):
            return selection_model.SelectionFlag.NoUpdate
        elif event.type() == QEvent.Type.MouseButtonRelease:
            return selection_model.SelectionFlag.ClearAndSelect
        else:
            return super(AudioSessionListView, self).selectionCommand(index, event)

    def selectionChanged(self, selected, deselected):
        super(AudioSessionListView, self).selectionChanged(selected, deselected)
        selected_indexes = selected.indexes()
        deselected_indexes = deselected.indexes()
        for session in (index.data(Qt.ItemDataRole.UserRole) for index in deselected_indexes):
            if session.client_conference is not None:
                for sibling in session.client_conference.sessions:
                    sibling.items.audio.widget.selected = False
            else:
                session.widget.selected = False
        for session in (index.data(Qt.ItemDataRole.UserRole) for index in selected_indexes):
            if session.client_conference is not None:
                for sibling in session.client_conference.sessions:
                    sibling.items.audio.widget.selected = True
            else:
                session.widget.selected = True
        if selected_indexes:
            self.setCurrentIndex(selected_indexes[0])
        else:
            self.setCurrentIndex(self.model().index(-1))
        self.context_menu.hide()
        # print "-- audio selection changed %s -> %s (ignore=%s)" % ([x.row() for x in deselected.indexes()], [x.row() for x in selected.indexes()], self.ignore_selection_changes)
        if self.ignore_selection_changes:
            return
        notification_center = NotificationCenter()
        selected_blink_session = selected[0].topLeft().data(Qt.ItemDataRole.UserRole).blink_session if selected else None
        deselected_blink_session = deselected[0].topLeft().data(Qt.ItemDataRole.UserRole).blink_session if deselected else None
        notification_data = NotificationData(selected_session=selected_blink_session, deselected_session=deselected_blink_session)
        notification_center.post_notification('BlinkSessionListSelectionChanged', sender=self, data=notification_data)
        if selected_blink_session is not None:
            self._update_control_menu(selected[0].topLeft().data(Qt.ItemDataRole.UserRole))

    def startDrag(self, supported_actions):
        if self._pressed_index is not None and self._pressed_index.isValid():
            self.dragged_session = self._pressed_index.data(Qt.ItemDataRole.UserRole)
            rect = self.visualRect(self._pressed_index)
            rect.adjust(1, 1, -1, -1)
            pixmap = QPixmap(rect.size())
            pixmap.fill(Qt.GlobalColor.transparent)
            widget = DraggedAudioSessionWidget(self.dragged_session.widget, None)
            widget.setFixedSize(rect.size())
            widget.render(pixmap)
            drag = QDrag(self)
            drag.setPixmap(pixmap)
            drag.setMimeData(self.model().mimeData([self._pressed_index]))
            drag.setHotSpot(self._pressed_position - rect.topLeft())
            drag.exec(supported_actions, Qt.DropAction.CopyAction)
            self.dragged_session = None
            self._pressed_position = None
            self._pressed_index = None

    def dragEnterEvent(self, event):
        event_source = event.source()
        accepted_mime_types = set(self.model().accepted_mime_types)
        provided_mime_types = set(event.mimeData().formats())
        acceptable_mime_types = accepted_mime_types & provided_mime_types
        if not acceptable_mime_types:
            event.ignore()
        elif event_source is not self and 'application/x-blink-session-list' in provided_mime_types:
            event.ignore()  # we don't handle drops for blink sessions from other sources
        else:
            event.accept()

    def dragLeaveEvent(self, event):
        super(AudioSessionListView, self).dragLeaveEvent(event)
        for session in self.model().sessions:
            session.widget.drop_indicator = False

    def dragMoveEvent(self, event):
        super(AudioSessionListView, self).dragMoveEvent(event)

        model = self.model()
        mime_data = event.mimeData()

        for session in model.sessions:
            session.widget.drop_indicator = False

        for mime_type in model.accepted_mime_types:
            if mime_data.hasFormat(mime_type):
                index = self.indexAt(event.pos())
                rect = self.visualRect(index)
                session = index.data(Qt.ItemDataRole.UserRole)
                name = mime_type.replace('/', ' ').replace('-', ' ').title().replace(' ', '')
                handler = getattr(self, '_DH_%s' % name)
                handler(event, index, rect, session)
                break
        else:
            event.ignore()

    def dropEvent(self, event):
        model = self.model()
        if event.source() is self:
            if event.keyboardModifiers() & Qt.KeyboardModifier.AltModifier:
                event.setDropAction(Qt.DropAction.LinkAction)
            else:
                event.setDropAction(Qt.DropAction.MoveAction)
        for session in self.model().sessions:
            session.widget.drop_indicator = False
        if model.handleDroppedData(event.mimeData(), event.dropAction(), self.indexAt(event.pos())):
            event.accept()
        super(AudioSessionListView, self).dropEvent(event)

    def _DH_ApplicationXBlinkSessionList(self, event, index, rect, session):
        dragged_session = self.dragged_session
        if not index.isValid():
            model = self.model()
            rect = self.viewport().rect()
            rect.setTop(self.visualRect(model.index(len(model.sessions)-1)).bottom())
            if dragged_session.client_conference is not None:
                event.setDropAction(Qt.DropAction.MoveAction)
                event.accept(rect)
            else:
                event.ignore(rect)
        elif event.keyboardModifiers() & Qt.KeyboardModifier.AltModifier and dragged_session.client_conference is None:
            if dragged_session is session or session.client_conference is not None or session.blink_session.state != 'connected':
                event.ignore(rect)
            elif dragged_session.blink_session.transfer_state in ('active', 'completed') or session.blink_session.transfer_state in ('active', 'completed'):
                event.ignore(rect)
            else:
                session.widget.drop_indicator = True
                event.setDropAction(Qt.DropAction.LinkAction)  # it might not be LinkAction if other keyboard modifiers are active
                event.accept(rect)
        else:
            conference = dragged_session.client_conference or Null
            if dragged_session is session or session.blink_session in conference.sessions:
                event.ignore(rect)
            else:
                if dragged_session.client_conference is None:
                    if session.client_conference is not None:
                        for sibling in session.client_conference.sessions:
                            sibling.items.audio.widget.drop_indicator = True
                    else:
                        session.widget.drop_indicator = True
                event.setDropAction(Qt.DropAction.MoveAction)
                event.accept(rect)

    def _DH_ApplicationXBlinkContactList(self, event, index, rect, session):
        model = self.model()
        if not index.isValid():
            rect = self.viewport().rect()
            rect.setTop(self.visualRect(model.index(len(model.sessions)-1)).bottom())
            event.ignore(rect)
        else:
            event.accept(rect)
            if session.client_conference is not None:
                for sibling in session.client_conference.sessions:
                    sibling.items.audio.widget.drop_indicator = True
            else:
                session.widget.drop_indicator = True

    def _DH_ApplicationXBlinkContactUriList(self, event, index, rect, session):
        model = self.model()
        if not index.isValid():
            rect = self.viewport().rect()
            rect.setTop(self.visualRect(model.index(len(model.sessions)-1)).bottom())
            event.ignore(rect)
        else:
            event.accept(rect)
            if session.client_conference is not None:
                for sibling in session.client_conference.sessions:
                    sibling.items.audio.widget.drop_indicator = True
            else:
                session.widget.drop_indicator = True

    def _AH_AddVideo(self):
        selected_indexes = self.selectedIndexes()
        if selected_indexes:
            session = selected_indexes[0].data(Qt.ItemDataRole.UserRole)
            if 'audio' in session.blink_session.streams:
                session.blink_session.add_stream(StreamDescription('video'))
            else:
                session.blink_session.add_streams([StreamDescription('video'), StreamDescription('audio')])

    def _AH_RemoveVideo(self):
        selected_indexes = self.selectedIndexes()
        if selected_indexes:
            session = selected_indexes[0].data(Qt.ItemDataRole.UserRole)
            session.blink_session.remove_stream(session.blink_session.streams.get('video'))

    def _AH_AddAudio(self):
        selected_indexes = self.selectedIndexes()
        if selected_indexes:
            session = selected_indexes[0].data(Qt.ItemDataRole.UserRole)
            session.blink_session.add_stream(StreamDescription('audio'))

    def _AH_RemoveAudio(self):
        selected_indexes = self.selectedIndexes()
        if selected_indexes:
            session = selected_indexes[0].data(Qt.ItemDataRole.UserRole)
            session.blink_session.remove_stream(session.blink_session.streams.get('audio'))

    def _AH_AddChat(self):
        selected_indexes = self.selectedIndexes()
        if selected_indexes:
            session = selected_indexes[0].data(Qt.ItemDataRole.UserRole)
            session.blink_session.add_stream(StreamDescription('chat'))

    def _AH_RemoveChat(self):
        selected_indexes = self.selectedIndexes()
        if selected_indexes:
            session = selected_indexes[0].data(Qt.ItemDataRole.UserRole)
            session.blink_session.remove_stream(session.blink_session.streams.get('chat'))

    def _AH_ShareMyScreen(self):
        selected_indexes = self.selectedIndexes()
        if selected_indexes:
            session = selected_indexes[0].data(Qt.ItemDataRole.UserRole)
            if 'audio' in session.blink_session.streams:
                session.blink_session.add_stream(StreamDescription('screen-sharing', mode='server'))
            else:
                session.blink_session.add_streams([StreamDescription('screen-sharing', mode='server'), StreamDescription('audio')])

    def _AH_RequestScreen(self):
        selected_indexes = self.selectedIndexes()
        if selected_indexes:
            session = selected_indexes[0].data(Qt.ItemDataRole.UserRole)
            if 'audio' in session.blink_session.streams:
                session.blink_session.add_stream(StreamDescription('screen-sharing', mode='viewer'))
            else:
                session.blink_session.add_streams([StreamDescription('screen-sharing', mode='viewer'), StreamDescription('audio')])

    def _AH_EndScreenSharing(self):
        selected_indexes = self.selectedIndexes()
        if selected_indexes:
            session = selected_indexes[0].data(Qt.ItemDataRole.UserRole)
            session.blink_session.remove_stream(session.blink_session.streams.get('screen-sharing'))

    def _AH_SendSMS(self):
        blink = QApplication.instance()
        blink.chat_window.show()

    def _AH_ShowSessionInfo(self):
        blink = QApplication.instance()
        blink.chat_window.show_session_info()

    def _AH_SendFiles(self, uri=None):
        selected_indexes = self.selectedIndexes()
        if selected_indexes:
            session = selected_indexes[0].data(Qt.ItemDataRole.UserRole)
            contact = session.blink_session.contact
            session_manager = SessionManager()
            for filename in QFileDialog.getOpenFileNames(self, translate('audio_session', 'Select File(s)'), session_manager.send_file_directory, 'Any file (*.*)')[0]:
                session_manager.send_file(contact, contact.uri, filename)

    def _SH_HangupShortcutActivated(self):
        selected_indexes = self.selectedIndexes()
        if selected_indexes:
            session = selected_indexes[0].data(Qt.ItemDataRole.UserRole)
            if session.client_conference is None:
                session.widget.hangup_button.click()

    def _SH_HoldShortcutActivated(self):
        selected_indexes = self.selectedIndexes()
        if selected_indexes:
            session = selected_indexes[0].data(Qt.ItemDataRole.UserRole)
            if session.client_conference is None:
                session.widget.hold_button.click()

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_BlinkActiveSessionDidChange(self, notification):
        self.ignore_selection_changes = True
        selection_model = self.selectionModel()
        if notification.data.active_session is None:
            selection = selection_model.selection()
            # check the code in this if branch if it's needed -Dan
            # selected_blink_session = selection[0].topLeft().data(Qt.ItemDataRole.UserRole).blink_session if selection else None
            # if notification.data.previous_active_session is selected_blink_session:
            #    print "-- audio session list updating selection to None None"
            #    selection_model.clearSelection()
        else:
            model = self.model()
            position = model.sessions.index(notification.data.active_session.items.audio)
            # print "-- audio session list updating selection to", position, notification.data.active_session
            selection_model.select(model.index(position), selection_model.SelectionFlag.ClearAndSelect)
        self.ignore_selection_changes = False


# Chat sessions
#

class ChatSessionIconLabel(QLabel):
    icon = QtDynamicProperty('icon', type=QIcon)
    selectedCompositionColor = QtDynamicProperty('selectedCompositionColor', type=QColor)

    def __init__(self, parent=None):
        super(ChatSessionIconLabel, self).__init__(parent)
        self.pixmaps = Container()
        self.icon = None
        self.icon_size = 12
        self.selectedCompositionColor = Qt.GlobalColor.transparent

    def event(self, event):
        if event.type() == QEvent.Type.DynamicPropertyChange and event.propertyName() in ('icon', 'selectedCompositionColor') and self.icon is not None:
            self.pixmaps.standard = self.icon.pixmap(self.icon_size)
            self.pixmaps.selected = QPixmap(self.pixmaps.standard)
            painter = QPainter(self.pixmaps.selected)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceAtop)
            painter.fillRect(self.pixmaps.selected.rect(), self.selectedCompositionColor)
            painter.end()
        return super(ChatSessionIconLabel, self).event(event)

    def paintEvent(self, event):
        if self.icon is None or self.icon.isNull():
            return
        session_widget = self.parent().parent()
        style = self.style()
        painter = QPainter(self)
        margin = self.margin()
        rect = self.contentsRect().adjusted(margin, margin, -margin, -margin)
        if not self.isEnabled():
            option = QStyleOption()
            option.initFrom(self)
            pixmap = style.generatedIconPixmap(QIcon.Mode.Disabled, self.pixmaps.standard, option)
        elif session_widget.display_mode is session_widget.SelectedDisplayMode:
            pixmap = self.pixmaps.selected
        else:
            pixmap = self.pixmaps.standard
        align = style.visualAlignment(self.layoutDirection(), self.alignment())
        style.drawItemPixmap(painter, rect, align, pixmap)


ui_class, base_class = uic.loadUiType(Resources.get('chat_session.ui'))


class ChatSessionWidget(base_class, ui_class):
    class StandardDisplayMode(metaclass=MarkerType):  pass
    class AlternateDisplayMode(metaclass=MarkerType): pass
    class SelectedDisplayMode(metaclass=MarkerType):  pass

    def __init__(self, parent=None):
        super(ChatSessionWidget, self).__init__(parent)
        with Resources.directory:
            self.setupUi(self)
        self.palettes = Container()
        self.palettes.standard = self.palette()
        self.palettes.alternate = self.palette()
        self.palettes.selected = self.palette()
        self.palettes.standard.setColor(QPalette.ColorRole.Window,  self.palettes.standard.color(QPalette.ColorRole.Base))           # We modify the palettes because only the Oxygen theme honors the BackgroundRole if set
        self.palettes.alternate.setColor(QPalette.ColorRole.Window, self.palettes.standard.color(QPalette.ColorRole.AlternateBase))  # AlternateBase set to #f0f4ff or #e0e9ff by designer
        self.palettes.selected.setColor(QPalette.ColorRole.Window,  self.palettes.standard.color(QPalette.ColorRole.Highlight))      # #0066cc #0066d5 #0066dd #0066aa (0, 102, 170) '#256182' (37, 97, 130), #2960a8 (41, 96, 168), '#2d6bbc' (45, 107, 188), '#245897' (36, 88, 151) #0044aa #0055d4
        self.setBackgroundRole(QPalette.ColorRole.Window)
        self.display_mode = self.StandardDisplayMode
        self.hold_icon.installEventFilter(self)
        self.composing_icon.installEventFilter(self)
        self.audio_icon.installEventFilter(self)
        self.chat_icon.installEventFilter(self)
        self.video_icon.installEventFilter(self)
        self.screen_sharing_icon.installEventFilter(self)
        self.widget_layout.invalidate()
        self.widget_layout.activate()
        # self.setAttribute(103) # Qt.WidgetAttribute.WA_DontShowOnScreen == 103 and is missing from pyqt, but is present in qt and pyside -Dan
        # self.show()

    def _get_display_mode(self):
        return self.__dict__['display_mode']

    def _set_display_mode(self, value):
        if value not in (self.StandardDisplayMode, self.AlternateDisplayMode, self.SelectedDisplayMode):
            raise ValueError("invalid display_mode: %r" % value)
        old_mode = self.__dict__.get('display_mode', None)
        new_mode = self.__dict__['display_mode'] = value
        if new_mode == old_mode:
            return
        if new_mode is self.StandardDisplayMode:
            self.setPalette(self.palettes.standard)
            self.setForegroundRole(QPalette.ColorRole.WindowText)
            self.name_label.setForegroundRole(QPalette.ColorRole.WindowText)
            self.info_label.setForegroundRole(QPalette.ColorRole.Dark)
        elif new_mode is self.AlternateDisplayMode:
            self.setPalette(self.palettes.alternate)
            self.setForegroundRole(QPalette.ColorRole.WindowText)
            self.name_label.setForegroundRole(QPalette.ColorRole.WindowText)
            self.info_label.setForegroundRole(QPalette.ColorRole.Dark)
        elif new_mode is self.SelectedDisplayMode:
            self.setPalette(self.palettes.selected)
            self.setForegroundRole(QPalette.ColorRole.HighlightedText)
            self.name_label.setForegroundRole(QPalette.ColorRole.HighlightedText)
            self.info_label.setForegroundRole(QPalette.ColorRole.HighlightedText)

    display_mode = property(_get_display_mode, _set_display_mode)
    del _get_display_mode, _set_display_mode

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.ShowToParent, QEvent.Type.HideToParent):
            self.widget_layout.invalidate()
            self.widget_layout.activate()
        return False

    def paintEvent(self, event):
        super(ChatSessionWidget, self).paintEvent(event)
        if self.display_mode is self.SelectedDisplayMode and self.state_label.state is not None:
            rect = self.state_label.geometry()
            rect.setWidth(self.width() - rect.x())
            gradient = QLinearGradient(0, 0, 1, 0)
            gradient.setCoordinateMode(QLinearGradient.CoordinateMode.ObjectBoundingMode)
            gradient.setColorAt(0.0, Qt.GlobalColor.transparent)
            gradient.setColorAt(1.0, Qt.GlobalColor.white)
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.fillRect(rect, QBrush(gradient))
            painter.end()

    def update_content(self, session):
        self.name_label.setText(session.name)
        self.info_label.setText(session.info)
        self.icon_label.setPixmap(session.pixmap)
        self.state_label.state = session.state
        self.hold_icon.setVisible(session.blink_session.on_hold)
        self.composing_icon.setVisible(session.remote_composing)
        self.chat_icon.setVisible('chat' in session.blink_session.streams)
        self.audio_icon.setVisible('audio' in session.blink_session.streams)
        self.video_icon.setVisible('video' in session.blink_session.streams)
        self.screen_sharing_icon.setVisible('screen-sharing' in session.blink_session.streams)
        self.unread_label.setVisible(bool(session.blink_session.contact.unread_messages))
        self.unread_label.setText(str(session.blink_session.contact.unread_messages))
        background_color = StateColor('#00ff00').stroke
        self.unread_label.setStyleSheet(f'color: #ffffff; padding: 4px; font-weight: bold; background-color: {background_color.name()}; border-radius: 4px;')

del ui_class, base_class


@implementer(IObserver)
class ChatSessionItem(object):

    size_hint = QSize(200, 36)

    def __init__(self, blink_session, timestamp=None):
        self.timestamp = timestamp or ISOTimestamp.now().replace(tzinfo=tzlocal())
        self.blink_session = blink_session
        self.blink_session.items.chat = self
        self.remote_composing = False
        self.remote_composing_timer = QTimer()
        self.remote_composing_timer.timeout.connect(self._SH_RemoteComposingTimerTimeout)
        self.participants_model = ConferenceParticipantModel(blink_session)
        self.files_model = FileListModel(blink_session)
        self.widget = ChatSessionWidget(None)
        self.widget.update_content(self)
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=blink_session)
        notification_center.add_observer(self, name='BlinkUnreadMessagesChanged')

    def __repr__(self):
        return '%s(%r)' % (self.__class__.__name__, self.blink_session)

    @property
    def name(self):
        return self.blink_session.contact.name

    @property
    def info(self):
        return self.blink_session.contact.note or self.blink_session.contact_uri.uri

    @property
    def state(self):
        return self.blink_session.contact.state

    @property
    def icon(self):
        return self.blink_session.contact.icon

    @property
    def pixmap(self):
        return self.blink_session.contact.pixmap

    @property
    def chat_stream(self):
        return self.blink_session.streams.get('chat')

    @property
    def messages_stream(self):
        return self.blink_session.fake_streams.get('messages')

    @property
    def audio_stream(self):
        return self.blink_session.streams.get('audio')

    @property
    def video_stream(self):
        return self.blink_session.streams.get('video')

    def _get_remote_composing(self):
        return self.__dict__['remote_composing']

    def _set_remote_composing(self, value):
        old_value = self.__dict__.get('remote_composing', False)
        self.__dict__['remote_composing'] = value
        if value != old_value and self.widget is not None:
            self.widget.composing_icon.setVisible(value)
            notification_center = NotificationCenter()
            notification_center.post_notification('ChatSessionItemDidChange', sender=self)

    remote_composing = property(_get_remote_composing, _set_remote_composing)
    del _get_remote_composing, _set_remote_composing

    def end(self, delete=False):
        self.blink_session.end(delete=delete)

    def delete(self):
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=self.blink_session)
        self.participants_model = None
        self.blink_session.items.chat = None
        self.blink_session = None
        self.widget = None

    def update_composing_indication(self, data):
        if data.state == 'active':
            self.remote_composing = True
            refresh_rate = data.refresh if data.refresh else 120
            self.remote_composing_timer.start(refresh_rate*1000)
        elif data.state == 'idle':
            self.remote_composing = False
            self.remote_composing_timer.stop()
        self.widget.update_content(self)

    def _SH_RemoteComposingTimerTimeout(self):
        self.remote_composing_timer.stop()
        self.remote_composing = False
        self.widget.update_content(self)

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_BlinkSessionContactDidChange(self, notification):
        self.widget.update_content(self)
        notification.center.post_notification('ChatSessionItemDidChange', sender=self)

    def _NH_BlinkSessionDidReinitializeForIncoming(self, notification):
        self.widget.update_content(self)
        notification.center.post_notification('ChatSessionItemDidChange', sender=self)

    def _NH_ChatSessionUnreadMessagesCountChanged(self, notification):
        self.widget.update_content(self)
        notification.center.post_notification('ChatSessionItemDidChange', sender=self)

    def _NH_BlinkUnreadMessagesChanged(self, notification):
        if self.widget:
            self.widget.update_content(self)
            notification.center.post_notification('ChatSessionItemDidChange', sender=self)

    def _NH_BlinkSessionDidReinitializeForOutgoing(self, notification):
        self.widget.update_content(self)
        notification.center.post_notification('ChatSessionItemDidChange', sender=self)

    def _NH_BlinkSessionWillConnect(self, notification):
        self.widget.chat_icon.setEnabled(False)
        self.widget.audio_icon.setEnabled(False)
        self.widget.video_icon.setEnabled(False)
        self.widget.screen_sharing_icon.setEnabled(False)
        self.widget.update_content(self)
        notification.center.post_notification('ChatSessionItemDidChange', sender=self)

    def _NH_BlinkSessionDidConnect(self, notification):
        self.widget.chat_icon.setEnabled(True)
        self.widget.audio_icon.setEnabled(True)
        self.widget.video_icon.setEnabled(True)
        self.widget.screen_sharing_icon.setEnabled(True)
        self.widget.update_content(self)
        notification.center.post_notification('ChatSessionItemDidChange', sender=self)

    def _NH_BlinkSessionWillAddStream(self, notification):
        if notification.data.stream.type == 'messages':
            return
        icon_label = getattr(self.widget, "%s_icon" % notification.data.stream.type.replace('-', '_'))
        icon_label.setEnabled(False)
        self.widget.update_content(self)
        notification.center.post_notification('ChatSessionItemDidChange', sender=self)

    def _NH_BlinkSessionDidAddStream(self, notification):
        icon_label = getattr(self.widget, "%s_icon" % notification.data.stream.type.replace('-', '_'))
        icon_label.setEnabled(True)
        self.widget.update_content(self)
        notification.center.post_notification('ChatSessionItemDidChange', sender=self)

    def _NH_BlinkSessionDidNotAddStream(self, notification):
        if notification.data.stream.type == 'messages':
            return
        icon_label = getattr(self.widget, "%s_icon" % notification.data.stream.type.replace('-', '_'))
        icon_label.setEnabled(True)
        self.widget.update_content(self)
        notification.center.post_notification('ChatSessionItemDidChange', sender=self)

    def _NH_BlinkSessionDidRemoveStream(self, notification):
        if notification.data.stream.type == 'chat':
            self.remote_composing = False
        self.widget.update_content(self)
        notification.center.post_notification('ChatSessionItemDidChange', sender=self)

    def _NH_BlinkSessionDidEnd(self, notification):
        self.remote_composing = False
        self.widget.update_content(self)
        notification.center.post_notification('ChatSessionItemDidChange', sender=self)

    def _NH_BlinkSessionDidChangeHoldState(self, notification):
        self.widget.hold_icon.setVisible(self.blink_session.on_hold)
        notification.center.post_notification('ChatSessionItemDidChange', sender=self)

    def _NH_BlinkSessionDidChangeRecordingState(self, notification):
        notification.center.post_notification('ChatSessionItemDidChange', sender=self)


class ChatSessionDelegate(QStyledItemDelegate, ColorHelperMixin):
    def __init__(self, parent=None):
        super(ChatSessionDelegate, self).__init__(parent)

    def editorEvent(self, event, model, option, index):
        if event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton and event.modifiers() == Qt.KeyboardModifier.NoModifier:
            arrow_rect = option.rect.adjusted(int(option.rect.width()-14), int(option.rect.height()/2), 0, 0)   # bottom half of the rightmost 14 pixels
            cross_rect = option.rect.adjusted(int(option.rect.width()-14), 0, 0, int(-option.rect.height()/2))  # top half of the rightmost 14 pixels
            if arrow_rect.contains(event.pos()):
                session_list = self.parent()
                session_list.animation.setDirection(QPropertyAnimation.Direction.Backward)
                session_list.animation.start()
                return True
            elif cross_rect.contains(event.pos()):
                session = index.data(Qt.ItemDataRole.UserRole)
                session.end(delete=True)
                return True
        return super(ChatSessionDelegate, self).editorEvent(event, model, option, index)

    def paint(self, painter, option, index):
        session = index.data(Qt.ItemDataRole.UserRole)
        if option.state & QStyle.StateFlag.State_Selected:
            session.widget.display_mode = session.widget.SelectedDisplayMode
        elif index.row() % 2 == 0:
            session.widget.display_mode = session.widget.StandardDisplayMode
        else:
            session.widget.display_mode = session.widget.AlternateDisplayMode
        session.widget.setFixedSize(option.rect.size())

        painter.save()
        painter.drawPixmap(option.rect, session.widget.grab())
        if option.state & QStyle.StateFlag.State_MouseOver:
            self.drawSessionIndicators(session, option, painter, session.widget)
        if 0 and (option.state & QStyle.StateFlag.State_MouseOver):
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            if option.state & QStyle.StateFlag.State_Selected:
                painter.fillRect(option.rect, QColor(240, 244, 255, 40))
            else:
                painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
                painter.fillRect(option.rect, QColor(240, 244, 255, 230))
        painter.restore()

    def drawSessionIndicators(self, session, option, painter, widget):
        pen_thickness = 1.6

        if widget.state_label.state is not None:
            foreground_color = option.palette.color(QPalette.ColorGroup.Normal, QPalette.ColorRole.WindowText)
            background_color = widget.state_label.state_colors[widget.state_label.state]
            base_contrast_color = self.calc_light_color(background_color)
            gradient = QLinearGradient(0, 0, 1, 0)
            gradient.setCoordinateMode(QLinearGradient.CoordinateMode.ObjectBoundingMode)
            gradient.setColorAt(0.0, self.color_with_alpha(base_contrast_color, 0.3*255))
            gradient.setColorAt(1.0, self.color_with_alpha(base_contrast_color, 0.8*255))
            contrast_color = QBrush(gradient)
        else:
            # foreground_color = option.palette.color(QPalette.ColorGroup.Normal, QPalette.ColorRole.WindowText)
            # background_color = option.palette.color(QPalette.ColorRole.Window)
            foreground_color = widget.palette().color(QPalette.ColorGroup.Normal, widget.foregroundRole())
            background_color = widget.palette().color(widget.backgroundRole())
            contrast_color = self.calc_light_color(background_color)
        line_color = self.deco_color(background_color, foreground_color)

        pen = QPen(line_color, pen_thickness, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        contrast_pen = QPen(contrast_color, pen_thickness, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)

        # draw the expansion indicator at the bottom (works best with a state_label of width 14)
        arrow_rect = QRect(0, 0, 14, 14)
        arrow_rect.moveBottomRight(widget.state_label.geometry().bottomRight())
        arrow_rect.translate(option.rect.topLeft())

        arrow = QPolygonF([QPointF(-3, -1.5), QPointF(0.5, 2.5), QPointF(4, -1.5)])
        arrow.translate(1, 1)

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        painter.translate(arrow_rect.center())
        painter.translate(0, +1)
        painter.setPen(contrast_pen)
        painter.drawPolyline(arrow)
        painter.translate(0, -1)
        painter.setPen(pen)
        painter.drawPolyline(arrow)
        painter.restore()

        # draw the close indicator at the top (works best with a state_label of width 14)
        cross_rect = QRect(0, 0, 14, 14)
        cross_rect.moveTopRight(widget.state_label.geometry().topRight())
        cross_rect.translate(option.rect.topLeft())

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        painter.translate(cross_rect.center())
        painter.translate(+1.5, +1)
        painter.translate(0, +1)
        painter.setPen(contrast_pen)
        painter.drawLine(-3, 3, 3, -3)
        painter.drawLine(-3, 3, 3, -3)
        painter.translate(0, -1)
        painter.setPen(pen)
        painter.drawLine(-3, -3, 3, 3)
        painter.drawLine(-3, 3, 3, -3)
        painter.restore()

    def sizeHint(self, option, index):
        return index.data(Qt.ItemDataRole.SizeHintRole)


@implementer(IObserver)
class ChatSessionModel(QAbstractListModel):

    sessionAboutToBeAdded = pyqtSignal(ChatSessionItem)
    sessionAboutToBeRemoved = pyqtSignal(ChatSessionItem)
    sessionAdded = pyqtSignal(ChatSessionItem)
    sessionRemoved = pyqtSignal(ChatSessionItem)

    # The MIME types we accept in drop operations, in the order they should be handled
    accepted_mime_types = ['application/x-blink-contact-list', 'text/uri-list']

    def __init__(self, parent=None):
        super(ChatSessionModel, self).__init__(parent)
        self.sessions = []

        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='BlinkSessionNewIncoming')
        notification_center.add_observer(self, name='BlinkSessionNewOutgoing')
        notification_center.add_observer(self, name='BlinkSessionWasDeleted')
        notification_center.add_observer(self, name='ChatSessionItemDidChange')

    def flags(self, index):
        if index.isValid():
            return QAbstractListModel.flags(self, index) | Qt.ItemFlag.ItemIsDropEnabled
        else:
            return QAbstractListModel.flags(self, index) | Qt.ItemFlag.ItemIsDropEnabled

    def rowCount(self, parent=QModelIndex()):
        return len(self.sessions)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        item = self.sessions[index.row()]
        if role == Qt.ItemDataRole.UserRole:
            return item
        elif role == Qt.ItemDataRole.SizeHintRole:
            return item.size_hint
        elif role == Qt.ItemDataRole.DisplayRole:
            return str(item)
        return None

    def supportedDropActions(self):
        return Qt.DropAction.CopyAction  # | Qt.DropAction.MoveAction

    def dropMimeData(self, mime_data, action, row, column, parent_index):
        # this is here just to keep the default Qt DnD API happy
        # the custom handler is in handleDroppedData
        return False

    def handleDroppedData(self, mime_data, action, index):
        if action == Qt.DropAction.IgnoreAction:
            return True

        for mime_type in self.accepted_mime_types:
            if mime_data.hasFormat(mime_type):
                name = mime_type.replace('/', ' ').replace('-', ' ').title().replace(' ', '')
                handler = getattr(self, '_DH_%s' % name)
                return handler(mime_data, action, index)
        else:
            return False

    def _DH_ApplicationXBlinkContactList(self, mime_data, action, index):
        return True

    def _DH_TextUriList(self, mime_data, action, index):
        return False

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_BlinkSessionNewIncoming(self, notification):
        self.addSession(ChatSessionItem(notification.sender))

    def _NH_BlinkSessionNewOutgoing(self, notification):
        self.addSession(ChatSessionItem(notification.sender))

    def _NH_BlinkSessionWasDeleted(self, notification):
        self.removeSession(notification.sender.items.chat)

    def _NH_ChatSessionItemDidChange(self, notification):
        index = self.index(self.sessions.index(notification.sender))
        self.dataChanged.emit(index, index)

    def _find_insertion_point(self, session):
        for position, item in enumerate(self.sessions):
            if session.timestamp > item.timestamp:
                break
        else:
            position = len(self.sessions)
        return position

    def _add_session(self, session):
        position = self._find_insertion_point(session)
        self.beginInsertRows(QModelIndex(), position, position)
        self.sessions.insert(position, session)
        self.endInsertRows()
        message_log.info(f'Initialize {session.blink_session.direction} session from {session.blink_session.account.id} to {session.blink_session.contact_uri.uri}')

    def _pop_session(self, session):
        position = self.sessions.index(session)
        self.beginRemoveRows(QModelIndex(), position, position)
        del self.sessions[position]
        self.endRemoveRows()
        return session

    def addSession(self, session):
        if session in self.sessions:
            return
        self.sessionAboutToBeAdded.emit(session)
        self._add_session(session)
        self.sessionAdded.emit(session)

    def removeSession(self, session):
        if session not in self.sessions:
            return
        self.sessionAboutToBeRemoved.emit(session)
        self._pop_session(session).delete()
        self.sessionRemoved.emit(session)


@implementer(IObserver)
class ChatSessionListView(QListView):

    def __init__(self, chat_window):
        super(ChatSessionListView, self).__init__(chat_window.session_panel)
        self.chat_window = chat_window
        self.setItemDelegate(ChatSessionDelegate(self))
        self.setMouseTracking(True)
        self.setAlternatingRowColors(True)
        self.setAutoFillBackground(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)  # default
        self.setDragEnabled(False) # default
        # self.setDropIndicatorShown(True)
        self.setDragDropMode(QListView.DragDropMode.DropOnly)
        self.setSelectionMode(QListView.SelectionMode.SingleSelection)  # default

        self.setStyleSheet("""QListView { border: 1px inset palette(dark); border-radius: 3px; }""")
        self.animation = QPropertyAnimation(self, b'geometry')
        self.animation.setDuration(250)
        #self.animation.setEasingCurve(QEasingCurve.Type.Linear)
        self.animation.finished.connect(self._SH_AnimationFinished)
        self.context_menu = QMenu(self)
        self.actions = ContextMenuActions()
        self.drop_indicator_index = QModelIndex()
        self.ignore_selection_changes = False
        self.doubleClicked.connect(self._SH_DoubleClicked)  # activated is emitted on single click
        chat_window.session_panel.installEventFilter(self)

        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='BlinkActiveSessionDidChange')

    def selectionChanged(self, selected, deselected):
        super(ChatSessionListView, self).selectionChanged(selected, deselected)
        selection_model = self.selectionModel()
        selection = selection_model.selection()
        if selection_model.currentIndex() not in selection:
            index = selection.indexes()[0] if not selection.isEmpty() else self.model().index(-1)
            selection_model.setCurrentIndex(index, selection_model.SelectionFlag.Select)
        self.context_menu.hide()
        if self.ignore_selection_changes:
            return
        notification_center = NotificationCenter()
        selected_blink_session = selected[0].topLeft().data(Qt.ItemDataRole.UserRole).blink_session if selected else None
        deselected_blink_session = deselected[0].topLeft().data(Qt.ItemDataRole.UserRole).blink_session if deselected else None
        notification_data = NotificationData(selected_session=selected_blink_session, deselected_session=deselected_blink_session)
        notification_center.post_notification('BlinkSessionListSelectionChanged', sender=self, data=notification_data)

    def selectionCommand(self, index, event=None):
        # in case we implement DnD later, we might consider selecting the item on mouse press if there is nothing else selected (except maybe if mouse press was on the buttons area?)
        # this would allow the dragged item to be selected before DnD starts, in case it is needed for the session to be active when dragged -Dan
        selection_model = self.selectionModel()
        if self.selectionMode() == self.SelectionMode.NoSelection:
            return selection_model.SelectionFlag.NoUpdate
        elif not index.isValid() or event is None:
            return selection_model.SelectionFlag.NoUpdate
        elif event.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseMove):
            return selection_model.SelectionFlag.NoUpdate
        elif event.type() == QEvent.Type.MouseButtonRelease:
            index_rect = self.visualRect(index)
            cross_rect = index_rect.adjusted(int(index_rect.width()-14), 0, 0, int(-index_rect.height()/2))  # the top half of the rightmost 14 pixels
            if cross_rect.contains(event.pos()):
                return selection_model.SelectionFlag.NoUpdate
            else:
                return selection_model.SelectionFlag.ClearAndSelect
        else:
            return super(ChatSessionListView, self).selectionCommand(index, event)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Resize:
            new_size = event.size()
            geometry = self.animation.endValue() if self.animation else None
            if geometry is not None:
                old_size = geometry.size()
                geometry.setSize(new_size)
                self.animation.setEndValue(geometry)
                geometry = self.animation.startValue()
                geometry.setWidth(geometry.width() + new_size.width() - old_size.width())
                self.animation.setStartValue(geometry)
            self.resize(new_size)
        return False

    def contextMenuEvent(self, event):
        pass

    def hideEvent(self, event):
        self.context_menu.hide()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape and self.selectionModel().selection():
            self.animation.setDirection(QPropertyAnimation.Backward)
            self.animation.start()
        else:
            super(ChatSessionListView, self).keyPressEvent(event)

    def paintEvent(self, event):
        super(ChatSessionListView, self).paintEvent(event)
        if self.drop_indicator_index.isValid():
            rect = self.visualRect(self.drop_indicator_index)
            painter = QPainter(self.viewport())
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QBrush(QColor('#dc3169')), 2.0))
            painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 3, 3)
            painter.end()

    def dragEnterEvent(self, event):
        model = self.model()
        accepted_mime_types = set(model.accepted_mime_types)
        provided_mime_types = set(event.mimeData().formats())
        acceptable_mime_types = accepted_mime_types & provided_mime_types
        if not acceptable_mime_types:
            event.ignore()
        else:
            event.accept()

    def dragLeaveEvent(self, event):
        super(ChatSessionListView, self).dragLeaveEvent(event)
        self.viewport().update(self.visualRect(self.drop_indicator_index))
        self.drop_indicator_index = QModelIndex()

    def dragMoveEvent(self, event):
        super(ChatSessionListView, self).dragMoveEvent(event)

        model = self.model()
        mime_data = event.mimeData()

        for mime_type in model.accepted_mime_types:
            if mime_data.hasFormat(mime_type):
                self.viewport().update(self.visualRect(self.drop_indicator_index))
                self.drop_indicator_index = QModelIndex()
                index = self.indexAt(event.pos())
                rect = self.visualRect(index)
                item = index.data(Qt.ItemDataRole.UserRole)
                name = mime_type.replace('/', ' ').replace('-', ' ').title().replace(' ', '')
                handler = getattr(self, '_DH_%s' % name)
                handler(event, index, rect, item)
                self.viewport().update(self.visualRect(self.drop_indicator_index))
                break
        else:
            event.ignore()

    def dropEvent(self, event):
        model = self.model()
        if model.handleDroppedData(event.mimeData(), event.dropAction(), self.indexAt(event.pos())):
            event.accept()
        super(ChatSessionListView, self).dropEvent(event)
        self.viewport().update(self.visualRect(self.drop_indicator_index))
        self.drop_indicator_index = QModelIndex()

    def _DH_ApplicationXBlinkContactList(self, event, index, rect, item):
        event.accept(rect)

    def _DH_TextUriList(self, event, index, rect, item):
        model = self.model()
        if not index.isValid():
            rect = self.viewport().rect()
            rect.setTop(self.visualRect(model.index(len(model.sessions)-1)).bottom())
        event.accept(rect)
        self.drop_indicator_index = index

    def _SH_AnimationFinished(self):
        if self.animation.direction() == QPropertyAnimation.Direction.Forward:
            try:
                self.scrollTo(self.selectedIndexes()[0], self.ScrollHint.EnsureVisible)
            except IndexError:
                pass
            self.setFocus(Qt.FocusReason.OtherFocusReason)
        else:
            current_tab = self.chat_window.tab_widget.currentWidget()
            current_tab.chat_input.setFocus(Qt.FocusReason.OtherFocusReason)
            self.hide()


    def _SH_DoubleClicked(self, index):
        self.animation.setDirection(QPropertyAnimation.Direction.Backward)
        self.animation.start()

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_BlinkActiveSessionDidChange(self, notification):
        self.ignore_selection_changes = True
        selection_model = self.selectionModel()
        if notification.data.active_session is None:
            selection = selection_model.selection()
            # check the code in this if branch if it's needed -Dan (if not also remove previous_active_session maybe)
            # selected_blink_session = selection[0].topLeft().data(Qt.ItemDataRole.UserRole).blink_session if selection else None
            # if notification.data.previous_active_session is selected_blink_session:
            #    print "-- chat session list updating selection to None None"
            #    selection_model.clearSelection()
        else:
            model = self.model()
            position = model.sessions.index(notification.data.active_session.items.chat)
            # print "-- chat session list updating selection to", position, notification.data.active_session
            selection_model.select(model.index(position), selection_model.SelectionFlag.ClearAndSelect)
        self.ignore_selection_changes = False


# Screen sharing
#

class VNCServerProcess(QProcess):
    __running__ = set()

    ready = pyqtSignal()

    def __init__(self, parent=None):
        super(VNCServerProcess, self).__init__(parent)
        self.server_host = 'localhost'
        self.server_port = None
        self.started.connect(self._SH_Started)
        self.error.connect(self._SH_Error)
        self.finished.connect(self._SH_Finished)
        self.readyReadStandardOutput.connect(self._SH_ReadyReadStandardOutput)
        self.readyReadStandardError.connect(self._SH_ReadyReadStandardError)

    @property
    def command(self):
        if sys.platform == 'win32':
            return '"%s"' % os.path.abspath(os.path.join(Resources.directory, '..', 'blinkvnc.exe'))
        else:
            return 'x11vnc -quiet -localhost -once -noshared -nopw -noncache -repeat'

    def start(self):
        super(VNCServerProcess, self).start(self.command)

    def _SH_Started(self):
        if self.server_port is not None:
            self.ready.emit()
        self.__running__.add(self)

    def _SH_Error(self, error):
        self.server_port = None

    def _SH_Finished(self, exit_code, exit_status):
        self.server_port = None
        self.__running__.remove(self)

    def _SH_ReadyReadStandardOutput(self):
        server_output = str(self.readAllStandardOutput())
        if self.server_port is None:
            match = re.search(r'^PORT\s*=\s*(?P<port>\d+)\s*$', server_output, re.IGNORECASE | re.MULTILINE)
            if match:
                self.server_port = int(match.group('port'))
                if self.state() == QProcess.ProcessState.Running:
                    self.ready.emit()

    def _SH_ReadyReadStandardError(self):
        self.readAllStandardError()


class ExternalVNCServerHandler(ExternalVNCServerHandler):
    address = property(lambda self: None if self.vnc_process.server_port is None else (self.vnc_process.server_host, self.vnc_process.server_port))

    def __init__(self):
        super(ExternalVNCServerHandler, self).__init__()
        self.vnc_process = None

    @run_in_gui_thread
    def _NH_MediaStreamDidStart(self, notification):
        self.vnc_process = VNCServerProcess()
        self.vnc_process.ready.connect(self._SH_VNCProcessReady)
        self.vnc_process.error.connect(self._SH_VNCProcessError)
        self.vnc_process.start()

    def _NH_MediaStreamWillEnd(self, notification):
        super(ExternalVNCServerHandler, self)._NH_MediaStreamWillEnd(notification)
        if self.vnc_process is not None:
            self.vnc_process.kill()

    @run_in_twisted_thread
    def _SH_VNCProcessReady(self):
        self.vnc_starter_thread = spawn(self._start_vnc_connection)

    @run_in_twisted_thread
    def _SH_VNCProcessError(self, error):
        NotificationCenter().post_notification('ScreenSharingHandlerDidFail', sender=self, data=NotificationData(context='starting', failure=None, reason='failed to start VNC server'))


class EmbeddedVNCViewerHandler(ExternalVNCViewerHandler):
    def __init__(self):
        super(EmbeddedVNCViewerHandler, self).__init__()
        self.window = None

    @run_in_gui_thread
    def _start_vnc_viewer(self):
        settings = BlinkSettings()
        notification_center = NotificationCenter()
        host, port = self.address
        vncclient = VNCClient(host, port, settings=ServerDefault)
        notification_center.add_observer(self, sender=vncclient)
        self.window = ScreensharingWindow(vncclient)
        self.window.show()
        if settings.screen_sharing.scale:
            self.window.scale_action.trigger()
        if settings.screen_sharing.open_viewonly:
            self.window.viewonly_action.trigger()
        if settings.screen_sharing.open_fullscreen:
            self.window.fullscreen_action.trigger()
        self.window.vncclient.start()

    @run_in_gui_thread
    def _stop_vnc_viewer(self):
        if self.window is not None:
            self.window.vncclient.stop()
            self.window.hide()

    @run_in_gui_thread
    def _NH_VNCClientDidEnd(self, notification):
        notification.center.remove_observer(self, sender=notification.sender)
        self.window = None

    def _NH_MediaStreamDidStart(self, notification):
        super(EmbeddedVNCViewerHandler, self)._NH_MediaStreamDidStart(notification)
        self._start_vnc_viewer()

    def _NH_MediaStreamWillEnd(self, notification):
        super(EmbeddedVNCViewerHandler, self)._NH_MediaStreamWillEnd(notification)
        self._stop_vnc_viewer()


ScreenSharingStream.ServerHandler = ExternalVNCServerHandler
ScreenSharingStream.ViewerHandler = EmbeddedVNCViewerHandler


# File transfers
#

class RandomID(metaclass=MarkerType): pass


class FileSizeFormatter(object):
    boundaries = [(             1024, '%d bytes',               1),
                  (          10*1024, '%.2f KB',           1024.0),  (        1024*1024, '%.1f KB',           1024.0),
                  (     10*1024*1024, '%.2f MB',      1024*1024.0),  (   1024*1024*1024, '%.1f MB',      1024*1024.0),
                  (10*1024*1024*1024, '%.2f GB', 1024*1024*1024.0),  (float('infinity'), '%.1f GB', 1024*1024*1024.0)]

    @classmethod
    def format(cls, size):
        for boundary, format, divisor in cls.boundaries:
            if size < boundary:
                return format % (size/divisor,)
        else:
            return "%d bytes" % size


@implementer(IObserver)
class BlinkFileTransfer(BlinkSessionBase):

    def __init__(self):
        self.id = None
        self.direction = None
        self.state = None

        self.account = None
        self.contact = None
        self.contact_uri = None
        self.sip_session = None
        self.stream = None
        self.remote_instance_id = None

        self.file_selector = None
        self.handler = None
        self.transfer_type = 'push'
        self.conference_file = True

        # used for outgoing transfers
        self._uri = None
        self._stat = None

        # Open file after download
        self.must_open = False

    def __establish__(self):
        notification_center = NotificationCenter()
        notification_center.post_notification('BlinkFileTransferWasCreated', sender=self)

    def __getstate__(self):
        # duplicate the selector, we cannot serialize the fd
        file_selector = FileSelector(name=self.file_selector.name, type=self.file_selector.type, size=self.file_selector.size, hash=self.file_selector.hash)
        state = dict(id=self.id, direction=self.direction, state=self.state, file_selector=file_selector)
        return self.account.id, self.contact.name, self.contact_uri.uri, state

    def __setstate__(self, state):
        from blink.contacts import URIUtils
        account_id, contact_name, contact_uri, state = state
        self.__init__()
        self.__dict__.update(state)
        account_manager = AccountManager()
        try:
            self.account = account_manager.get_account(account_id)
        except KeyError:
            self.account = account_manager.default_account
        self.contact, self.contact_uri = URIUtils.find_contact(contact_uri, display_name=contact_name)
        if self.direction == 'outgoing':
            self._uri = self._normalize_uri(contact_uri)

    @property
    def filename(self):
        return self.file_selector.name if self.file_selector is not None else None

    @property
    def hash(self):
        return self.file_selector.hash if self.file_selector is not None else None

    def init_incoming(self, contact, contact_uri, session, stream):
        assert self.state is None
        self.direction = 'incoming'

        self.id = stream.transfer_id

        self.account = session.account
        self.contact = contact
        self.contact_uri = contact_uri
        self.sip_session = session
        self.stream = stream

        self.file_selector = stream.file_selector
        self.handler = stream.handler

        self.state = 'connecting'
        notification_center = NotificationCenter()
        notification_center.post_notification('BlinkFileTransferNewIncoming', sender=self)
        self.sip_session.accept([self.stream])

    def init_outgoing(self, account, contact, contact_uri, filename, transfer_id=RandomID):
        assert self.state is None
        self.direction = 'outgoing'

        self.id = transfer_id if transfer_id is not RandomID else str(uuid.uuid4())

        self.account = account
        self.contact = contact
        self.contact_uri = contact_uri

        self._uri = self._normalize_uri(contact_uri.uri)

        self.file_selector = FileSelector.for_file(filename)
        self._stat = os.fstat(self.file_selector.fd.fileno())

        if self.file_selector.type.startswith('image'):
            directory = ApplicationData.get(f'downloads/{self.id}')
            makedirs(directory)
            shutil.copy(filename, directory)

        self.state = 'initialized'
        notification_center = NotificationCenter()
        notification_center.post_notification('BlinkFileTransferNewOutgoing', self)

        if self._stat.st_size < 26214400:
            stream = StreamDescription('messages')
            message_stream = stream.create_stream()
            message_stream.blink_session = self
            if account.sms.enable_pgp and account.sms.private_key is not None and os.path.exists(account.sms.private_key.normalized):
                message_stream.enable_pgp()

            if message_stream.can_encrypt:
                self.state = 'encrypting'
                notification_center.add_observer(self, name='PGPFileDidEncrypt')
                message_stream.encrypt_file(filename, self)

    def init_outgoing_pull(self, account, contact, contact_uri, filename, hash, transfer_id=RandomID, conference_file=True, must_open=False):
        assert self.state is None
        self.transfer_type = 'pull'
        self.direction = 'outgoing'
        self.must_open = must_open

        self.conference_file = conference_file

        self.id = transfer_id if transfer_id is not RandomID else str(uuid.uuid4())

        self.account = account
        self.contact = contact
        self.contact_uri = contact_uri
        self._hash = hash
        self._uri = self._normalize_uri(contact_uri.uri)

        self.file_selector = FileSelector(name=os.path.basename(filename), hash=hash)

        self.state = 'initialized'
        notification_center = NotificationCenter()
        notification_center.post_notification('BlinkFileTransferNewOutgoing', self)

    def connect(self):
        assert self.direction == 'outgoing' and self.state in ('initialized', 'ended', 'encrypting', 'encrypted')

        if self.state == 'encrypting':
            return

        notification_center = NotificationCenter()

        if self.state == 'ended':
            # Reinitialize to retry
            file_selector = FileSelector.for_file(self.filename)
            stat = os.fstat(file_selector.fd.fileno())
            if self._stat is not None and stat.st_mtime == self._stat.st_mtime:
                file_selector.hash = self.file_selector.hash
            if self.transfer_type == 'pull':
                file_selector.hash = self._hash

            self.file_selector = file_selector
            self._stat = stat
            self.state = 'initialized'
            notification_center.post_notification('BlinkFileTransferWillRetry', self)

        settings = SIPSimpleSettings()
        if isinstance(self.account, Account):
            if self.account.sip.outbound_proxy is not None:
                uri = SIPURI(host=self.account.sip.outbound_proxy.host, port=self.account.sip.outbound_proxy.port, parameters={'transport': self.account.sip.outbound_proxy.transport})
            elif self.account.sip.always_use_my_proxy:
                uri = SIPURI(host=self.account.id.domain)
            else:
                uri = self._uri
        else:
            uri = self._uri

        lookup = DNSLookup()
        notification_center.add_observer(self, sender=lookup)
        lookup.lookup_sip_proxy(uri, settings.sip.transport_list, tls_name=self.account.sip.tls_name or uri.host)

        self.state = 'connecting/dns_lookup'

    def end(self):
        assert self.state is not None
        if self.state in ('ending', 'ended'):
            return
        self.state = 'ending'
        if self.sip_session is not None:
            self.sip_session.end()
        else:
            assert self.direction == 'outgoing'
            self._terminate(failure_reason='Cancelled')

    def _get_state(self):
        return self.__dict__['state']

    def _set_state(self, value):
        if value is not None and not isinstance(value, SessionState):
            value = SessionState(value)
        old_state = self.__dict__.get('state', None)
        new_state = self.__dict__['state'] = value
        if new_state != old_state:
            NotificationCenter().post_notification('BlinkFileTransferDidChangeState', sender=self, data=NotificationData(old_state=old_state, new_state=new_state))

    state = property(_get_state, _set_state)
    del _get_state, _set_state

    def _get_sip_session(self):
        return self.__dict__['sip_session']

    def _set_sip_session(self, value):
        old_session = self.__dict__.get('sip_session', None)
        new_session = self.__dict__['sip_session'] = value
        if new_session != old_session:
            notification_center = NotificationCenter()
            if old_session is not None:
                notification_center.remove_observer(self, sender=old_session)
            if new_session is not None:
                notification_center.add_observer(self, sender=new_session)

    sip_session = property(_get_sip_session, _set_sip_session)
    del _get_sip_session, _set_sip_session

    def _get_stream(self):
        return self.__dict__['stream']

    def _set_stream(self, value):
        old_stream = self.__dict__.get('stream', None)
        new_stream = self.__dict__['stream'] = value
        if new_stream != old_stream:
            notification_center = NotificationCenter()
            if old_stream is not None:
                notification_center.remove_observer(self, sender=old_stream)
            if new_stream is not None:
                notification_center.add_observer(self, sender=new_stream)

    stream = property(_get_stream, _set_stream)
    del _get_stream, _set_stream

    def _get_handler(self):
        return self.__dict__['handler']

    def _set_handler(self, value):
        old_handler = self.__dict__.get('handler', None)
        new_handler = self.__dict__['handler'] = value
        if new_handler != old_handler:
            notification_center = NotificationCenter()
            if old_handler is not None:
                notification_center.remove_observer(self, sender=old_handler)
            if new_handler is not None:
                notification_center.add_observer(self, sender=new_handler)

    handler = property(_get_handler, _set_handler)
    del _get_handler, _set_handler

    def _normalize_uri(self, uri):
        if '@' not in uri:
            uri += '@' + self.account.id.domain
        if not uri.startswith(('sip:', 'sips:')):
            uri = 'sip:' + uri
        return SIPURI.parse(str(uri).translate(translation_table))

    def _terminate(self, failure_reason=None):
        self.state = 'ending'    # if the state is not ending already, simulate it
        self.sip_session = None
        self.stream = None
        self.handler = None

        if failure_reason is not None:
            end_reason = failure_reason
        else:
            end_reason = translate('sessions', 'Completed (%s)') % FileSizeFormatter.format(self.file_selector.size)
        state = SessionState('ended')
        state.reason = end_reason
        state.error = failure_reason is not None

        self.state = state
        notification_center = NotificationCenter()
        notification_center.post_notification('BlinkFileTransferDidEnd', sender=self, data=NotificationData(id=self.id, filename=self.file_selector.name, reason=state.reason, error=state.error, must_open=self.must_open))

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_DNSLookupDidSucceed(self, notification):
        notification.center.remove_observer(self, sender=notification.sender)
        if self.state in ('ending', 'ended'):
            return
        self.state = 'connecting'
        routes = notification.data.result
        if not routes:
            self._terminate(failure_reason=translate('sessions', 'Domain not found in DNS'))
            self.routes = None
            return
        self.routes = routes
        self.sip_session = Session(self.account)
        self.stream = MediaStreamRegistry.FileTransferStream(self.file_selector, 'sendonly', transfer_id=self.id)

        if self.transfer_type == 'pull':
            self.stream = MediaStreamRegistry.FileTransferStream(self.file_selector, 'recvonly', transfer_id=self.id)
        self.handler = self.stream.handler

        if not self.conference_file:
            directory = ApplicationData.get(f'downloads/{self.id}')
            makedirs(directory)
            self.handler.save_directory = directory

        self.sip_session.connect(ToHeader(self._uri), routes, [self.stream])

    def _NH_DNSLookupDidFail(self, notification):
        notification.center.remove_observer(self, sender=notification.sender)
        if self.state in ('ending', 'ended'):
            return
        self._terminate(failure_reason=translate('sessions', 'DNS Lookup failed'))

    def _NH_SIPSessionNewOutgoing(self, notification):
        self.state = 'initializing'

    def _NH_SIPSessionGotProvisionalResponse(self, notification):
        if notification.data.code == 180:
            self.state = 'connecting/ringing'

    def _NH_SIPSessionWillStart(self, notification):
        self.state = 'connecting/starting'

    def _NH_SIPSessionDidStart(self, notification):
        self.state = 'connected'

    def _NH_PGPFileDidEncrypt(self, notification):
        if notification.sender is not self:
            return

        notification.center.remove_observer(self, name='PGPFileDidEncrypt')

        encrypted_file = io.BytesIO(notification.data.contents.__str__().encode())
        self.file_selector.fd = encrypted_file
        self.file_selector.name = notification.data.filename
        encrypted_file.seek(0, os.SEEK_END)
        self.file_selector.size = encrypted_file.tell()
        encrypted_file.seek(0)

        self.state = 'encrypted'
        self.connect()

    def _NH_PGPFileDidDecrypt(self, notification):
        if notification.sender is not self:
            return

        notification.center.remove_observer(self, name='PGPFileDidDecrypt')
        notification.center.remove_observer(self, name='PGPFileDidNotDecrypt')

        self.file_selector.name = notification.data.filename
        notification.center.post_notification('BlinkFileTransferDidDecrypt',
                                              sender=self,
                                              data=NotificationData(reason='File decrypted', filename=notification.data.filename, must_open=notification.data.must_open, id=notification.data.id))
        if not notification.data.must_open:
            self._terminate(failure_reason=None)

    def _NH_PGPFileDidNotDecrypt(self, notification):
        if notification.sender is not self:
            return

        notification.center.remove_observer(self, name='PGPFileDidDecrypt')
        notification.center.remove_observer(self, name='PGPFileDidNotDecrypt')

        self.file_selector.name = notification.data.filename
        notification.center.post_notification('BlinkFileTransferDidNotDecrypt',
                                              sender=self,
                                              data=NotificationData(reason='File not decrypted'))
        self._terminate(failure_reason=notification.data.error)

    def _NH_MediaStreamDidInitialize(self, notification):
        notification.center.post_notification('BlinkFileTransferDidInitialize', sender=self)

    def _NH_MediaStreamDidNotInitialize(self, notification):
        self._terminate(failure_reason=notification.data.reason)

    def _NH_FileTransferHandlerHashProgress(self, notification):
        progress = int(notification.data.processed * 100 / notification.data.total)
        notification.center.post_notification('BlinkFileTransferHashProgress', sender=self, data=NotificationData(progress=progress))

    def _NH_FileTransferHandlerProgress(self, notification):
        notification.center.post_notification('BlinkFileTransferProgress', sender=self, data=NotificationData(bytes=notification.data.transferred_bytes, total_bytes=notification.data.total_bytes))

    def _NH_FileTransferHandlerDidEnd(self, notification):
        if self.direction == 'incoming':
            call_later(3, self.sip_session.end)
        else:
            self.sip_session.end()
        if notification.data.error:
            failure_reason = notification.data.reason or 'Failed'
        else:
            failure_reason = None

        if self.direction == 'outgoing' and self.transfer_type == 'push':
            self._terminate(failure_reason=failure_reason)
            return

        if self.file_selector.name.endswith('.asc') and not notification.data.error:
            stream = StreamDescription('messages')
            message_stream = stream.create_stream()
            message_stream.blink_session = self
            if self.account.sms.enable_pgp and self.account.sms.private_key is not None and os.path.exists(self.account.sms.private_key.normalized):
                message_stream.enable_pgp()

            if message_stream.can_decrypt:
                state = SessionState('decrypting')
                state.reason = ''
                state.error = None
                self.state = state

            notification_center = NotificationCenter()
            notification_center.add_observer(self, name='PGPFileDidDecrypt')
            notification_center.add_observer(self, name='PGPFileDidNotDecrypt')

            message_stream.decrypt_file(self.file_selector.name, self, id=self.id)
            return

        self._terminate(failure_reason=failure_reason)


class TransferStateLabel(QLabel, ColorHelperMixin):
    class ProgressDisplayMode(metaclass=MarkerType): pass
    class InactiveDisplayMode(metaclass=MarkerType): pass

    def __init__(self, parent=None):
        super(TransferStateLabel, self).__init__(parent)
        self.display_mode = self.InactiveDisplayMode
        self.show_cancel_button = False
        self.show_retry_button = False
        self.progress = 0

    def _get_display_mode(self):
        return self.__dict__['display_mode']

    def _set_display_mode(self, value):
        if value not in (self.ProgressDisplayMode, self.InactiveDisplayMode):
            raise ValueError("invalid display_mode: %r" % value)
        old_value = self.__dict__.get('display_mode', self.InactiveDisplayMode)
        new_value = self.__dict__['display_mode'] = value
        if new_value != old_value:
            self.update()

    display_mode = property(_get_display_mode, _set_display_mode)
    del _get_display_mode, _set_display_mode

    def _get_show_cancel_button(self):
        return self.__dict__['show_cancel_button']

    def _set_show_cancel_button(self, value):
        old_value = self.__dict__.get('show_cancel_button', False)
        new_value = self.__dict__['show_cancel_button'] = bool(value)
        if new_value != old_value:
            self.update()

    show_cancel_button = property(_get_show_cancel_button, _set_show_cancel_button)
    del _get_show_cancel_button, _set_show_cancel_button

    def _get_show_retry_button(self):
        return self.__dict__['show_retry_button']

    def _set_show_retry_button(self, value):
        old_value = self.__dict__.get('show_retry_button', False)
        new_value = self.__dict__['show_retry_button'] = bool(value)
        if new_value != old_value:
            self.update()

    show_retry_button = property(_get_show_retry_button, _set_show_retry_button)
    del _get_show_retry_button, _set_show_retry_button

    def _get_progress(self):
        return self.__dict__['progress']

    def _set_progress(self, value):
        self.__dict__['progress'] = value
        self.update()

    progress = property(_get_progress, _set_progress)
    del _get_progress, _set_progress

    def paintEvent(self, event):
        margin = self.margin()
        contents_rect = QRectF(self.contentsRect().adjusted(margin, margin, -margin, -margin))
        size = min(contents_rect.width(), contents_rect.height())
        rect = QRectF(0, 0, size, size)
        rect.moveCenter(contents_rect.center())
        palette = self.palette()
        if self.display_mode is self.ProgressDisplayMode:
            is_selected = self.foregroundRole() == QPalette.ColorRole.HighlightedText
            inner_pen_color = self.color_with_alpha(palette.color(self.foregroundRole()), 70)
            outer_pen_color = palette.color(QPalette.ColorRole.HighlightedText if is_selected else QPalette.ColorRole.Highlight)
            inner_pen_width = 1.4 * size/20
            outer_pen_width = 2.3 * size/20
            inner_rect_adjust = outer_pen_width - inner_pen_width / 2
            outer_rect_adjust = outer_pen_width / 2
            inner_rect = rect.adjusted(inner_rect_adjust, inner_rect_adjust, -inner_rect_adjust, -inner_rect_adjust)
            outer_rect = rect.adjusted(outer_rect_adjust, outer_rect_adjust, -outer_rect_adjust, -outer_rect_adjust)
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(QPen(inner_pen_color, inner_pen_width, style=Qt.PenStyle.SolidLine, cap=Qt.PenCapStyle.FlatCap, join=Qt.PenJoinStyle.RoundJoin))
            painter.drawEllipse(inner_rect)
            painter.setPen(QPen(outer_pen_color, outer_pen_width, style=Qt.PenStyle.SolidLine, cap=Qt.PenCapStyle.FlatCap, join=Qt.PenJoinStyle.RoundJoin))
            painter.drawArc(outer_rect, 90 * 16, int(-self.progress * 3.6 * 16))
            if self.show_cancel_button:
                foreground_color = palette.color(self.foregroundRole())
                background_color = palette.color(self.backgroundRole())
                cross_pen_color = self.strong_deco_color(background_color, foreground_color)
                cross_pen_width = 2.0 * size/20
                cross_rect = QRectF(0, 0, 6.0*size/20, 6.0*size/20)
                cross_rect.moveCenter(QPointF(0, 0))
                painter.save()
                painter.translate(rect.center())
                painter.setPen(QPen(cross_pen_color, cross_pen_width, style=Qt.PenStyle.SolidLine, cap=Qt.PenCapStyle.RoundCap, join=Qt.PenJoinStyle.RoundJoin))
                painter.drawLine(cross_rect.topLeft(), cross_rect.bottomRight())
                painter.drawLine(cross_rect.topRight(), cross_rect.bottomLeft())
                painter.restore()
            painter.end()
        elif self.show_retry_button:
            foreground_color = palette.color(self.foregroundRole())
            background_color = palette.color(self.backgroundRole())
            retry_pen_color = self.strong_deco_color(background_color, foreground_color)
            retry_pen_width = 1.8 * size/20
            retry_margin = 1.8 * size/20
            retry_rect_adjust = retry_pen_width / 2 + retry_margin
            retry_rect = rect.adjusted(retry_rect_adjust, retry_rect_adjust, -retry_rect_adjust, -retry_rect_adjust)
            path = QPainterPath()
            path.moveTo(retry_rect.width(), retry_rect.height()/2)
            path.arcMoveTo(retry_rect, -30)
            path.arcTo(retry_rect, -30, -300)
            arrow_size = path.currentPosition().y() - retry_rect.top() + min(retry_pen_width, retry_margin) / 2
            polygon = QPolygonF([QPointF(-arrow_size, 0), QPointF(0, 0), QPointF(0, -arrow_size)])
            polygon.translate(path.currentPosition())
            path.addPolygon(polygon)
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(QPen(retry_pen_color, retry_pen_width, style=Qt.PenStyle.SolidLine, cap=Qt.PenCapStyle.RoundCap, join=Qt.PenJoinStyle.MiterJoin))
            painter.drawPath(path)
            painter.end()

    @cache_result(background_color_key)
    def strong_deco_color(self, background, color):
        return ColorUtils.mix(background, color, 0.55 + 0.8*self._contrast)

    def minimumSizeHint(self):
        margin = self.margin()
        return QSize(16+2*margin, 16+2*margin)

    def sizeHint(self):
        margin = self.margin()
        return QSize(20+2*margin, 20+2*margin)


ui_class, base_class = uic.loadUiType(Resources.get('filetransfer_item.ui'))


class FileTransferItemWidget(base_class, ui_class):
    class StandardDisplayMode(metaclass=MarkerType):  pass
    class AlternateDisplayMode(metaclass=MarkerType): pass
    class SelectedDisplayMode(metaclass=MarkerType):  pass

    def __init__(self, parent=None):
        super(FileTransferItemWidget, self).__init__(parent)
        with Resources.directory:
            self.setupUi(self)
        self.palettes = Container()
        self.palettes.standard = self.palette()
        self.palettes.alternate = self.palette()
        self.palettes.selected = self.palette()
        self.palettes.standard.setColor(QPalette.ColorRole.Window,  self.palettes.standard.color(QPalette.ColorRole.Base))           # We modify the palettes because only the Oxygen theme honors the BackgroundRole if set
        self.palettes.alternate.setColor(QPalette.ColorRole.Window, self.palettes.standard.color(QPalette.ColorRole.AlternateBase))  # AlternateBase set to #f0f4ff or #e0e9ff by designer
        self.palettes.selected.setColor(QPalette.ColorRole.Window,  self.palettes.standard.color(QPalette.ColorRole.Highlight))      # #0066cc #0066d5 #0066dd #0066aa (0, 102, 170) '#256182' (37, 97, 130), #2960a8 (41, 96, 168), '#2d6bbc' (45, 107, 188), '#245897' (36, 88, 151) #0044aa #0055d4

        self.pixmaps = Container()
        self.pixmaps.incoming_transfer = QPixmap(Resources.get('icons/folder-downloads.png'))
        self.pixmaps.outgoing_transfer = QPixmap(Resources.get('icons/folder-uploads.png'))
        self.pixmaps.failed_transfer = QPixmap(Resources.get('icons/file-broken.png'))

        self.display_mode = self.StandardDisplayMode

        self.widget_layout.invalidate()
        self.widget_layout.activate()

    def _get_display_mode(self):
        return self.__dict__['display_mode']

    def _set_display_mode(self, value):
        if value not in (self.StandardDisplayMode, self.AlternateDisplayMode, self.SelectedDisplayMode):
            raise ValueError("invalid display_mode: %r" % value)
        old_mode = self.__dict__.get('display_mode', None)
        new_mode = self.__dict__['display_mode'] = value
        if new_mode == old_mode:
            return
        if new_mode is self.StandardDisplayMode:
            self.setPalette(self.palettes.standard)
            self.state_indicator.setForegroundRole(QPalette.ColorRole.WindowText)
            self.filename_label.setForegroundRole(QPalette.ColorRole.WindowText)
            self.name_label.setForegroundRole(QPalette.ColorRole.Dark)
            self.status_label.setForegroundRole(QPalette.ColorRole.Dark)
        elif new_mode is self.AlternateDisplayMode:
            self.setPalette(self.palettes.alternate)
            self.state_indicator.setForegroundRole(QPalette.ColorRole.WindowText)
            self.filename_label.setForegroundRole(QPalette.ColorRole.WindowText)
            self.name_label.setForegroundRole(QPalette.ColorRole.Dark)
            self.status_label.setForegroundRole(QPalette.ColorRole.Dark)
        elif new_mode is self.SelectedDisplayMode:
            self.setPalette(self.palettes.selected)
            self.state_indicator.setForegroundRole(QPalette.ColorRole.HighlightedText)
            self.filename_label.setForegroundRole(QPalette.ColorRole.HighlightedText)
            self.name_label.setForegroundRole(QPalette.ColorRole.HighlightedText)
            self.status_label.setForegroundRole(QPalette.ColorRole.HighlightedText)

    display_mode = property(_get_display_mode, _set_display_mode)
    del _get_display_mode, _set_display_mode

    def update_content(self, item, initial=False):
        if initial:
            if item.direction == 'outgoing' and item.transfer.transfer_type != 'pull':
                self.name_label.setText('To: ' + item.name)
                self.icon_label.setPixmap(self.pixmaps.outgoing_transfer)
            else:
                self.name_label.setText('From: ' + item.name)
                self.icon_label.setPixmap(self.pixmaps.incoming_transfer)
        self.filename_label.setText(os.path.basename(item.filename))
        self.status_label.setText(item.status)
        if item.ended:
            self.state_indicator.display_mode = self.state_indicator.InactiveDisplayMode
            self.state_indicator.show_retry_button = False
            if item.failed:
                if item.direction == 'outgoing':
                    self.state_indicator.show_retry_button = True
                self.icon_label.setPixmap(self.pixmaps.failed_transfer)
        else:
            self.state_indicator.display_mode = self.state_indicator.ProgressDisplayMode
            self.state_indicator.progress = item.progress or 0

del ui_class, base_class


@implementer(IObserver)
class FileTransferItem(object):

    def __init__(self, transfer):
        self.transfer = transfer

        self.status = None
        self.progress = None
        self.bytes = 0
        self.total_bytes = 0

        self.widget = FileTransferItemWidget()
        self.widget.update_content(self, initial=True)

        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=transfer)

    def __getstate__(self):
        state = self.__dict__.copy()
        del state['widget']
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        self.widget = FileTransferItemWidget()
        self.widget.update_content(self, initial=True)

    def __repr__(self):
        return '%s(%r)' % (self.__class__.__name__, self.transfer)

    def retry(self):
        assert self.direction == 'outgoing' and self.ended and self.failed
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=self.transfer)
        self.transfer.connect()

    def end(self):
        self.transfer.end()

    @property
    def direction(self):
        return self.transfer.direction

    @property
    def filename(self):
        return self.transfer.filename or ''

    @property
    def name(self):
        return self.transfer.contact.name or self.transfer.contact_uri.uri

    @property
    def ended(self):
        return self.transfer.state == 'ended'

    @property
    def failed(self):
        return self.transfer.state == 'ended' and self.transfer.state.error

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_BlinkFileTransferDidChangeState(self, notification):
        state = notification.data.new_state
        if state == 'connecting/dns_lookup':
            self.status = translate('sessions', 'Looking up destination...')
        elif state == 'connecting':
            self.status = translate('sessions', 'Connecting...')
        elif state == 'connecting/ringing':
            self.status = translate('sessions', 'Ringing...')
        elif state == 'connecting/starting':
            self.status = translate('sessions', 'Starting...')
        elif state == 'connected':
            self.status = translate('sessions', 'Connected')
        elif state == 'ending':
            self.status = translate('sessions', 'Ending...')
        elif state == 'decrypting':
            self.status = translate('sessions', 'Decrypting...')
        elif state == 'encrypting':
            self.status = translate('sessions', 'Encrypting...')
        elif state == 'encrypted':
            self.status = translate('sessions', 'Encrypted...')
        else:
            self.status = None
        self.progress = None
        self.widget.update_content(self)
        notification.center.post_notification('FileTransferItemDidChange', sender=self)

    def _NH_BlinkFileTransferDidInitialize(self, notification):
        self.progress = None
        self.status = translate('sessions', 'Connecting...')
        self.widget.update_content(self)
        notification.center.post_notification('FileTransferItemDidChange', sender=self)

    def _NH_BlinkFileTransferHashProgress(self, notification):
        progress = notification.data.progress
        if self.progress is None or progress > self.progress:
            self.progress = progress
            self.status = translate('sessions', 'Computing hash (%s%%)') % notification.data.progress
            self.widget.update_content(self)
            notification.center.post_notification('FileTransferItemDidChange', sender=self)

    def _NH_BlinkFileTransferProgress(self, notification):
        self.bytes = notification.data.bytes
        self.total_bytes = notification.data.total_bytes
        progress = int(self.bytes * 100 / self.total_bytes)
        status = translate('sessions', 'Transferring: %s/%s (%s%%)') % (FileSizeFormatter.format(self.bytes), FileSizeFormatter.format(self.total_bytes), progress)
        if self.progress is None or progress > self.progress or status != self.status:
            self.progress = progress
            self.status = status
            self.widget.update_content(self)
            notification.center.post_notification('FileTransferItemDidChange', sender=self)

    def _NH_BlinkFileTransferWillRetry(self, notification):
        self.status = None
        self.progress = None
        self.bytes = 0
        self.total_bytes = 0
        self.widget.update_content(self, initial=True)

    def _NH_BlinkFileTransferDidEnd(self, notification):
        self.status = notification.data.reason
        self.widget.update_content(self)
        notification.center.post_notification('FileTransferItemDidChange', sender=self)
        notification.center.remove_observer(self, sender=self.transfer)

    def _NH_BlinkFileTransferDidDecrypt(self, notification):
        self.status = notification.data.reason
        self.widget.update_content(self)
        notification.center.post_notification('FileTransferItemDidChange', sender=self)
        if notification.data.must_open:
            QDesktopServices.openUrl(QUrl.fromLocalFile(notification.data.filename))

    def _NH_BlinkFileTransferDidNotDecrypt(self, notification):
        self.status = notification.data.reason
        self.widget.update_content(self)
        notification.center.post_notification('FileTransferItemDidChange', sender=self)

    def _NH_BlinkFileTransferDidEncrypt(self, notification):
        self.status = notification.data.reason
        self.widget.update_content(self)
        notification.center.post_notification('FileTransferItemDidChange', sender=self)
        notification.center.remove_observer(self, sender=self.transfer)


class FileTransferDelegate(QStyledItemDelegate):
    def __init__(self, parent=None):
        super(FileTransferDelegate, self).__init__(parent)

    def editorEvent(self, event, model, option, index):
        if event.type() == QEvent.Type.MouseButtonDblClick and event.button() == Qt.MouseButton.LeftButton and event.modifiers() == Qt.KeyboardModifier.NoModifier:
            item = index.data(Qt.ItemDataRole.UserRole)
            if item.ended and not item.failed:
                QDesktopServices.openUrl(QUrl.fromLocalFile(item.filename))
                return True
            elif item.direction == 'outgoing' and not item.ended:
                item = index.data(Qt.ItemDataRole.UserRole)
                indicator = item.widget.state_indicator
                margin = indicator.margin()
                indicator_rect = indicator.contentsRect().adjusted(margin, margin, -margin, -margin)
                size = min(indicator_rect.width(), indicator_rect.height())
                rect = QRect(0, 0, size, size)
                rect.moveCenter(indicator.geometry().center())
                rect.translate(option.rect.topLeft())
                if not rect.contains(event.pos()):
                    QDesktopServices.openUrl(QUrl.fromLocalFile(item.filename))
                    return True
        elif event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton and event.modifiers() == Qt.KeyboardModifier.NoModifier:
            item = index.data(Qt.ItemDataRole.UserRole)
            indicator = item.widget.state_indicator
            margin = indicator.margin()
            indicator_rect = indicator.contentsRect().adjusted(margin, margin, -margin, -margin)
            size = min(indicator_rect.width(), indicator_rect.height())
            rect = QRect(0, 0, size, size)
            rect.moveCenter(indicator.geometry().center())
            rect.translate(option.rect.topLeft())
            if rect.contains(event.pos()):
                if indicator.display_mode is indicator.InactiveDisplayMode and indicator.show_retry_button:
                    item.retry()
                    return True
                elif indicator.display_mode is indicator.ProgressDisplayMode and indicator.show_cancel_button:
                    item.end()
                    return True
        return super(FileTransferDelegate, self).editorEvent(event, model, option, index)

    def paint(self, painter, option, index):
        item = index.data(Qt.ItemDataRole.UserRole)
        if option.state & QStyle.StateFlag.State_Selected:
            item.widget.display_mode = item.widget.SelectedDisplayMode
        elif index.row() % 2 == 0:
            item.widget.display_mode = item.widget.StandardDisplayMode
        else:
            item.widget.display_mode = item.widget.AlternateDisplayMode

        if not item.ended and (option.state & QStyle.StateFlag.State_MouseOver):
            item.widget.state_indicator.show_cancel_button = True
        else:
            item.widget.state_indicator.show_cancel_button = False

        item.widget.setFixedSize(option.rect.size())
        painter.drawPixmap(option.rect, item.widget.grab())

    def sizeHint(self, option, index):
        return index.data(Qt.ItemDataRole.SizeHintRole)


class TransferHistory(object):
    max_items = 100

    def __init__(self):
        self._transaction_level = 0
        self._pending_items = []

    @contextlib.contextmanager
    def transaction(self):
        self.begin_transaction()
        try:
            yield
        finally:
            self.commit_transaction()

    def begin_transaction(self):
        self._transaction_level += 1

    def commit_transaction(self):
        self._transaction_level -= 1
        if self._transaction_level == 0:
            self.save(self._pending_items)

    def load(self):
        try:
            items = pickle.load(open(ApplicationData.get('transfer_history')))
        except Exception:
            items = []
        return items

    def save(self, items):
        if self._transaction_level == 0:
            @run_in_thread('file-io')
            def save(items):
                with open(ApplicationData.get('transfer_history'), 'wb+') as f:
                    pickle.dump(items, f)
            save(items[:self.max_items])
            self._pending_items = []
        else:
            self._pending_items = items


@implementer(IObserver)
class FileTransferModel(QAbstractListModel):

    itemAboutToBeAdded = pyqtSignal(FileTransferItem)
    itemAboutToBeRemoved = pyqtSignal(FileTransferItem)
    itemAdded = pyqtSignal(FileTransferItem)
    itemRemoved = pyqtSignal(FileTransferItem)

    def __init__(self, parent=None):
        super(FileTransferModel, self).__init__(parent)
        self.items = []
        self.history = TransferHistory()
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='BlinkFileTransferNewIncoming')
        notification_center.add_observer(self, name='BlinkFileTransferNewOutgoing')
        notification_center.add_observer(self, name='BlinkFileTransferDidEnd')
        notification_center.add_observer(self, name='FileTransferItemDidChange')
        notification_center.add_observer(self, name='SIPApplicationDidStart')

    @property
    def ended_items(self):
        return [item for item in self.items if item.ended]

    def clear_ended(self):
        with self.history.transaction():
            for item in self.ended_items:
                self.removeItem(item)

    def rowCount(self, parent=QModelIndex()):
        return len(self.items)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        item = self.items[index.row()]
        if role == Qt.ItemDataRole.UserRole:
            return item
        elif role == Qt.ItemDataRole.SizeHintRole:
            return item.widget.sizeHint()
        elif role == Qt.ItemDataRole.DisplayRole:
            return str(item)
        return None

    def addItem(self, item):
        if item in self.items:
            return
        self.itemAboutToBeAdded.emit(item)
        self.beginInsertRows(QModelIndex(), 0, 0)
        self.items.insert(0, item)
        self.endInsertRows()
        self.itemAdded.emit(item)

    def removeItem(self, item):
        if item not in self.items:
            return
        self.itemAboutToBeRemoved.emit(item)
        position = self.items.index(item)
        self.beginRemoveRows(QModelIndex(), position, position)
        del self.items[position]
        self.endRemoveRows()
        self.itemRemoved.emit(item)
        self.history.save(self.ended_items)

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_BlinkFileTransferNewIncoming(self, notification):
        transfer = notification.sender
        with self.history.transaction():
            for item in (item for item in self.items if item.failed and item.direction == 'incoming'):
                if item.transfer.contact == transfer.contact and item.transfer.hash == transfer.hash:
                    self.removeItem(item)
                    break
            self.addItem(FileTransferItem(transfer))

    def _NH_BlinkFileTransferNewOutgoing(self, notification):
        self.addItem(FileTransferItem(notification.sender))

    def _NH_BlinkFileTransferDidEnd(self, notification):
        self.history.save(self.ended_items)

    def _NH_FileTransferItemDidChange(self, notification):
        index = self.index(self.items.index(notification.sender))
        self.dataChanged.emit(index, index)

    def _NH_SIPApplicationDidStart(self, notification):
        self.beginResetModel()
        self.items = self.history.load()
        self.endResetModel()


ui_class, base_class = uic.loadUiType(Resources.get('filelist_item.ui'))


# Chat file list

class FileListItemWidget(base_class, ui_class):
    class StandardDisplayMode(metaclass=MarkerType):  pass
    class AlternateDisplayMode(metaclass=MarkerType): pass
    class SelectedDisplayMode(metaclass=MarkerType):  pass

    def __init__(self, parent=None):
        super(FileListItemWidget, self).__init__(parent)
        with Resources.directory:
            self.setupUi(self)
        self.palettes = Container()
        self.palettes.standard = self.palette()
        self.palettes.alternate = self.palette()
        self.palettes.selected = self.palette()
        self.palettes.standard.setColor(QPalette.ColorRole.Window,  self.palettes.standard.color(QPalette.ColorRole.Base))           # We modify the palettes because only the Oxygen theme honors the BackgroundRole if set
        self.palettes.alternate.setColor(QPalette.ColorRole.Window, self.palettes.standard.color(QPalette.ColorRole.AlternateBase))  # AlternateBase set to #f0f4ff or #e0e9ff by designer
        self.palettes.selected.setColor(QPalette.ColorRole.Window,  self.palettes.standard.color(QPalette.ColorRole.Highlight))      # #0066cc #0066d5 #0066dd #0066aa (0, 102, 170) '#256182' (37, 97, 130), #2960a8 (41, 96, 168), '#2d6bbc' (45, 107, 188), '#245897' (36, 88, 151) #0044aa #0055d4

        self.pixmaps = Container()
        self.pixmaps.encrypted_transfer = QPixmap(Resources.get('icons/lock-green-18.svg'))

        self.display_mode = self.StandardDisplayMode

        self.widget_layout.invalidate()
        self.widget_layout.activate()

    def _get_display_mode(self):
        return self.__dict__['display_mode']

    def _set_display_mode(self, value):
        if value not in (self.StandardDisplayMode, self.AlternateDisplayMode, self.SelectedDisplayMode):
            raise ValueError("invalid display_mode: %r" % value)
        old_mode = self.__dict__.get('display_mode', None)
        new_mode = self.__dict__['display_mode'] = value
        if new_mode == old_mode:
            return
        if new_mode is self.StandardDisplayMode:
            self.setPalette(self.palettes.standard)
            self.state_indicator.setForegroundRole(QPalette.ColorRole.WindowText)
            self.filename_label.setForegroundRole(QPalette.ColorRole.WindowText)
            self.name_label.setForegroundRole(QPalette.ColorRole.Dark)
            self.status_label.setForegroundRole(QPalette.ColorRole.Dark)
            self.filesize_label.setForegroundRole(QPalette.ColorRole.Dark)
        elif new_mode is self.AlternateDisplayMode:
            self.setPalette(self.palettes.alternate)
            self.state_indicator.setForegroundRole(QPalette.ColorRole.WindowText)
            self.filename_label.setForegroundRole(QPalette.ColorRole.WindowText)
            self.name_label.setForegroundRole(QPalette.ColorRole.Dark)
            self.status_label.setForegroundRole(QPalette.ColorRole.Dark)
            self.filesize_label.setForegroundRole(QPalette.ColorRole.Dark)
        elif new_mode is self.SelectedDisplayMode:
            self.setPalette(self.palettes.selected)
            self.state_indicator.setForegroundRole(QPalette.ColorRole.HighlightedText)
            self.filename_label.setForegroundRole(QPalette.ColorRole.HighlightedText)
            self.name_label.setForegroundRole(QPalette.ColorRole.HighlightedText)
            self.status_label.setForegroundRole(QPalette.ColorRole.HighlightedText)
            self.filesize_label.setForegroundRole(QPalette.ColorRole.HighlightedText)

    display_mode = property(_get_display_mode, _set_display_mode)
    del _get_display_mode, _set_display_mode

    def update_content(self, item, initial=False):
        if initial:
            if item.direction == 'outgoing':
                self.name_label.setText('To: ' + item.name)
            else:
                self.name_label.setText('From: ' + item.name)
        icon = QFileIconProvider().icon(QFileInfo(item.decrypted_filename))

        self.icon_label.setPixmap(icon.pixmap(64))
        self.filename_label.setText(os.path.basename(item.decrypted_filename))
        self.filesize_label.setText('%s' % FileSizeFormatter.format(item.size))
        if item.encrypted:
            self.state_indicator.setPixmap(self.pixmaps.encrypted_transfer)
        
        if item.downloading:
            self.status_label.setText(item.status)
        elif item.download_error:
            self.status_label.setText(item.download_error)
        else:
            if item.until is not None and not item.in_conference:
                if item.expired:
                    self.status_label.setText(translate('chat_window', "Expired: %s" % item.until.strftime('%d %b %Y %H:%M')))
                    self.setToolTip(translate('chat_window', "Item still available in cache or local"))
                    self.status_label.setStyleSheet('color: red')
                else:
                    self.status_label.setText(translate('chat_window', "Expiry: %s" % item.until.strftime('%d %b %Y %H:%M')))
            else:
                self.status_label.setVisible(False)


del ui_class, base_class


@implementer(IObserver)
class FileListItem(object):

    def __init__(self, file, direction="incoming", id=None, conference_file=False):
        self.file = file
        self._direction = direction
        self._id = id
        self.in_conference = conference_file
        self.downloading = False
        self.download_error = None

        self.status = None
        self.progress = None
        self.bytes = 0
        self.total_bytes = 0

        self.widget = FileListItemWidget()
        self.widget.update_content(self, initial=True)

        notification_center = NotificationCenter()
        notification_center.add_observer(ObserverWeakrefProxy(self), name='BlinkHTTPTransferProgress')
        notification_center.add_observer(ObserverWeakrefProxy(self), name='BlinkHTTPTransferFailed')
        notification_center.add_observer(ObserverWeakrefProxy(self), name='BlinkHTTPTransferCompleted')
        
    def __getstate__(self):
        state = self.__dict__.copy()
        del state['widget']
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        self.widget = FileListItemWidget()
        self.widget.update_content(self, initial=True)

    def __repr__(self):
        return '%s(%r)' % (self.__class__.__name__, self.file)

    def __eq__(self, other):
        try:
            return self.id == other.id
        except AttributeError:
            return self.id == other

    @property
    def direction(self):
        return self._direction

    @property
    def filename(self):
        return self.file.name or ''

    @property
    def name(self):
        return self.file.contact.name or self.file.contact_uri.uri

    @property
    def size(self):
        return self.file.size

    @property
    def hash(self):
        return self.file.hash

    @property
    def encrypted(self):
        return self.file.encrypted

    @property
    def until(self):
        return self.file.until or ''

    @property
    def expired(self):
        return self.until != '' and self.until < datetime.now(timezone.utc)

    @property
    def decrypted_filename(self):
        return self.file.decrypted_filename

    @property
    def already_exists(self):
        return self.file.already_exists

    @property
    def id(self):
        try:
            return self.file.id
        except AttributeError:
            return self.id

    @property
    def account(self):
        return self.file.account

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_BlinkHTTPTransferProgress(self, notification):
        if notification.sender != self.file:
            return

        self.bytes = notification.data.bytes
        self.total_bytes = notification.data.total_bytes
        progress = int(self.bytes * 100 / self.total_bytes)
        status = translate('sessions', 'Transferring: %s/%s (%s%%)') % (FileSizeFormatter.format(self.bytes), FileSizeFormatter.format(self.total_bytes), progress)
        if progress == 100:
            self.downloading = False
            self.download_error = None
        else:
            self.downloading = True

        if self.progress is None or progress > self.progress or status != self.status:
            self.progress = progress
            self.status = status
            self.widget.update_content(self)
            notification.center.post_notification('HTTPFileTransferItemDidChange', data=NotificationData(file=self))

    def _NH_BlinkHTTPTransferFailed(self, notification):
        if notification.sender != self.file:
            return

        self.status = None
        self.progress = None
        self.downloading = False
        self.download_error = notification.data.reason
        self.widget.update_content(self)
        notification.center.post_notification('HTTPFileTransferItemDidChange', data=NotificationData(file=self))

    def _NH_BlinkHTTPTransferCompleted(self, notification):
        if notification.sender != self.file:
            return

        self.status = None
        self.progress = None
        self.bytes = 0
        self.downloading = False
        self.download_error = None
        self.widget.update_content(self)
        notification.center.post_notification('HTTPFileTransferItemDidChange', data=NotificationData(file=self))
            

@implementer(IObserver)
class FileListModel(QAbstractListModel):

    itemAboutToBeAdded = pyqtSignal(FileListItem)
    itemAboutToBeRemoved = pyqtSignal(FileListItem)
    itemAdded = pyqtSignal(FileListItem)
    itemRemoved = pyqtSignal(FileListItem)

    def __init__(self, session, parent=None):
        super(FileListModel, self).__init__(parent)
        self.session = session
        self.items = []
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='BlinkSessionDidShareFile')
        notification_center.add_observer(self, name='BlinkSessionShouldDownloadFile')
        notification_center.add_observer(self, name='BlinkGotMessageDelete')
        notification_center.add_observer(self, name='BlinkMessageWillDelete')
        notification_center.add_observer(self, name='BlinkConversationWillRemove')
        notification_center.add_observer(self, name='SIPApplicationDidStart')
        notification_center.add_observer(self, name='HTTPFileTransferItemDidChange')
        
    @property
    def ended_items(self):
        return [item for item in self.items if item.ended]

    def clear_ended(self):
        with self.history.transaction():
            for item in self.ended_items:
                self.removeItem(item)

    def rowCount(self, parent=QModelIndex()):
        return len(self.items)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        item = self.items[index.row()]
        if role == Qt.ItemDataRole.UserRole:
            return item
        elif role == Qt.ItemDataRole.SizeHintRole:
            return item.widget.sizeHint()
        elif role == Qt.ItemDataRole.DisplayRole:
            return str(item)
        return None

    def addItem(self, item):
        if item in self.items:
            return
        self.itemAboutToBeAdded.emit(item)
        self.beginInsertRows(QModelIndex(), 0, 0)
        self.items.insert(0, item)
        self.endInsertRows()
        self.itemAdded.emit(item)

    def deleteItem(self, id):
        if id not in self.items:
            return
        item = [item for item in self.items if item.id == id][0]
        NotificationCenter().post_notification('BlinkSessionShouldDeleteFile', sender=self.session, data=NotificationData(item=item))

    def removeItem(self, id):
        if id not in self.items:
            return
        item = [item for item in self.items if item.id == id][0]
        self.itemAboutToBeRemoved.emit(item)
        position = self.items.index(item)
        self.beginRemoveRows(QModelIndex(), position, position)
        del self.items[position]
        self.endRemoveRows()
        self.itemRemoved.emit(item)

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_HTTPFileTransferItemDidChange(self, notification):
        try:
            index = self.index(self.items.index(notification.data.file))
            self.dataChanged.emit(index, index)
        except ValueError:
            pass

    def _NH_BlinkMessageWillDelete(self, notification):
        blink_session = notification.sender
        session = blink_session.items.chat

        if session is None:
            return

        self.removeItem(notification.data.id)

    def _NH_BlinkGotMessageDelete(self, notification):
        blink_session = notification.sender
        session = blink_session.items.chat

        if session is None:
            return

        self.removeItem(notification.data)

    def _NH_BlinkSessionDidShareFile(self, notification):
        if self.session != notification.sender:
            return

        if self.session.remote_focus:
            if notification.data.file.protocol == 'msrp':
                self.addItem(FileListItem(notification.data.file, direction=notification.data.direction, conference_file=True))
            return

        self.addItem(FileListItem(notification.data.file, direction=notification.data.direction))

    def _NH_BlinkSessionShouldDownloadFile(self, notification):
        session = notification.sender
        id = notification.data.id
        if id in self.items:
            item = [item for item in self.items if item.id == id][0]
            if item.hash and item.file.protocol == 'msrp':
                SessionManager().get_file(session.contact, session.contact_uri, item.file.original_name, item.hash, item.id, account=item.account)
            else:
                SessionManager().get_file_from_url(session, item.file)

    def _NH_BlinkFileTransferNewIncoming(self, notification):
        transfer = notification.sender
        with self.history.transaction():
            for item in (item for item in self.items if item.failed and item.direction == 'incoming'):
                if item.transfer.contact == transfer.contact and item.transfer.hash == transfer.hash:
                    self.removeItem(item)
                    break
            self.addItem(FileTransferItem(transfer))

    def _NH_BlinkFileTransferNewOutgoing(self, notification):
        self.addItem(FileTransferItem(notification.sender))

    def _NH_FileTransferItemDidChange(self, notification):
        index = self.index(self.items.index(notification.sender))
        self.dataChanged.emit(index, index)

    def _NH_BlinkConversationWillRemove(self, notification):
        blink_session = notification.sender
        session = blink_session.items.chat

        if session is None:
            return

        for item in self.items:
            self.removeItem(item.id)

    def _NH_SIPApplicationDidStart(self, notification):
        self.beginResetModel()
        self.items = self.history.load()
        self.endResetModel()


class FileListDelegate(QStyledItemDelegate):
    def __init__(self, parent=None):
        super(FileListDelegate, self).__init__(parent)

    def editorEvent(self, event, model, option, index):
        if event.type() == QEvent.Type.MouseButtonDblClick and event.button() == Qt.MouseButton.LeftButton and event.modifiers() == Qt.KeyboardModifier.NoModifier:
            item = index.data(Qt.ItemDataRole.UserRole)
            if item.already_exists:
                directory = SIPSimpleSettings().file_transfer.directory.normalized
                link = copy_transfer_file(QUrl.fromLocalFile(item.decrypted_filename), directory)
                QDesktopServices.openUrl(link)
                return True

            if item.expired:
                return True

            if item.hash and item.file.protocol == 'msrp':
                SessionManager().get_file(model.session.contact, model.session.contact_uri, item.filename, item.hash, item.id, account=item.account)
            else:
                SessionManager().get_file_from_url(model.session, item.file)
            return True
        return super(FileListDelegate, self).editorEvent(event, model, option, index)

    def paint(self, painter, option, index):
        item = index.data(Qt.ItemDataRole.UserRole)
        if option.state & QStyle.StateFlag.State_Selected:
            item.widget.display_mode = item.widget.SelectedDisplayMode
        elif index.row() % 2 == 0:
            item.widget.display_mode = item.widget.StandardDisplayMode
        else:
            item.widget.display_mode = item.widget.AlternateDisplayMode

        item.widget.setFixedSize(option.rect.size())
        painter.drawPixmap(option.rect, item.widget.grab())

    def sizeHint(self, option, index):
        return index.data(Qt.ItemDataRole.SizeHintRole)


class FileListView(QListView, ColorHelperMixin):
    def __init__(self, parent=None):
        super(FileListView, self).__init__(parent)
        self.setItemDelegate(FileListDelegate(self))
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.context_menu = QMenu(self)
        self.actions = ContextMenuActions()
        self.actions.open_file = QAction(translate('filelist_view', "Open"), self, triggered=self._AH_OpenFile)
        self.actions.download_file = QAction(translate('filelist_view', "Download again"), self, triggered=self._AH_DownloadFile)
        self.actions.resume_download_file = QAction(translate('filelist_view', "Resume download"), self, triggered=self._AH_DownloadFile)
        self.actions.cancel_download_file = QAction(translate('filelist_view', "Cancel download"), self, triggered=self._AH_CancelDownloadFile)
        self.actions.open_downloads_folder = QAction(translate('filelist_view', "Open Transfers Folder"), self, triggered=self._AH_OpenTransfersFolder)
        self.actions.remove_file = QAction(translate('filelist_view', "Remove File"), self, triggered=self._AH_RemoveFile)
        self.paint_drop_indicator = False

    def setModel(self, model):
        selection_model = self.selectionModel()
        if selection_model is not None:
            selection_model.deleteLater()
        super(FileListView, self).setModel(model)

    def contextMenuEvent(self, event):
        menu = self.context_menu
        menu.clear()
        index = self.indexAt(event.pos())
        if index.isValid():
            item = index.data(Qt.ItemDataRole.UserRole)
            menu.addAction(self.actions.open_file)
            if not item.downloading:
                temporary_download_file = "%s.download" % item.filename
                if not os.path.exists(temporary_download_file):            
                    menu.addAction(self.actions.download_file)
                else:
                    menu.addAction(self.actions.resume_download_file)
            else:
                menu.addAction(self.actions.cancel_download_file)
            menu.addAction(self.actions.open_downloads_folder)
            if not item.in_conference:
                menu.addAction(self.actions.remove_file)
        menu.exec(self.mapToGlobal(event.pos()))

    def hideEvent(self, event):
        self.context_menu.hide()

    def paintEvent(self, event):
        super(FileListView, self).paintEvent(event)
        if self.paint_drop_indicator:
            rect = self.viewport().rect()  # or should this be self.contentsRect() ? -Dan
            # color = QColor('#b91959')
            # color = QColor('#00aaff')
            # color = QColor('#55aaff')
            # color = QColor('#00aa00')
            # color = QColor('#aa007f')
            # color = QColor('#dd44aa')
            color = QColor('#aa007f')
            pen_color = self.color_with_alpha(color, 120)
            brush_color = self.color_with_alpha(color, 10)
            painter = QPainter(self.viewport())
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setBrush(brush_color)
            painter.setPen(QPen(pen_color, 1.6))
            painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 3, 3)
            painter.end()

    def dragEnterEvent(self, event):
        model = self.model()
        accepted_mime_types = set(model.accepted_mime_types)
        provided_mime_types = set(event.mimeData().formats())
        acceptable_mime_types = accepted_mime_types & provided_mime_types
        if not acceptable_mime_types:
            event.ignore()
        else:
            event.accept()

    def dragLeaveEvent(self, event):
        super(ConferenceParticipantListView, self).dragLeaveEvent(event)
        self.paint_drop_indicator = False
        self.viewport().update()

    def dragMoveEvent(self, event):
        super(ConferenceParticipantListView, self).dragMoveEvent(event)

        model = self.model()
        mime_data = event.mimeData()

        for mime_type in model.accepted_mime_types:
            if mime_data.hasFormat(mime_type):
                handler = getattr(self, '_DH_%s' % mime_type.replace('/', ' ').replace('-', ' ').title().replace(' ', ''))
                handler(event)
                self.viewport().update()
                break
        else:
            event.ignore()

    def dropEvent(self, event):
        model = self.model()
        if model.handleDroppedData(event.mimeData(), event.dropAction(), self.indexAt(event.pos())):
            event.accept()
        super(ConferenceParticipantListView, self).dropEvent(event)
        self.paint_drop_indicator = False
        self.viewport().update()

    def _DH_ApplicationXBlinkContactList(self, event):
        event.accept(self.viewport().rect())
        self.paint_drop_indicator = True

    def _DH_ApplicationXBlinkContactUriList(self, event):
        event.accept(self.viewport().rect())
        self.paint_drop_indicator = True

    def _DH_TextUriList(self, event):
        event.ignore(self.viewport().rect())
        # event.accept(self.viewport().rect())
        # self.paint_drop_indicator = True

    def _AH_OpenFile(self, must_open=True):
        item = self.selectedIndexes()[0].data(Qt.ItemDataRole.UserRole)
        model = self.model()
        if item.already_exists:
            QDesktopServices.openUrl(QUrl.fromLocalFile(item.decrypted_filename))
            return True

        if item.downloading:
            message_log.info('Download still in progress')
            return

        if item.hash and item.file.protocol == 'msrp':
            if os.path.exists(item.decrypted_filename):
                QDesktopServices.openUrl(QUrl.fromLocalFile(item.decrypted_filename))
            else:
                SessionManager().get_file(model.session.contact, model.session.contact_uri, item.filename, item.hash, item.id, account=item.account)
        else:
            if item.expired:
                return True

            if not os.path.exists(item.filename):
                SessionManager().get_file_from_url(model.session, item.file, must_open=True)
            else:
                if item.encrypted and not os.path.exists(item.decrypted_filename):
                    stream = StreamDescription('messages')
                    message_stream = stream.create_stream()
                    message_stream.blink_session = model.session
                    if model.session.account.sms.enable_pgp and model.session.account.sms.private_key is not None and os.path.exists(model.session.account.sms.private_key.normalized):
                        message_stream.enable_pgp()

                    message_stream.decrypt_file(item.filename, model.session, id=item.file.id, must_open=True)
        return True

    def _AH_CancelDownloadFile(self):
        item = self.selectedIndexes()[0].data(Qt.ItemDataRole.UserRole)
        notification_center = NotificationCenter()
        notification_center.post_notification('BlinkHTTPTransferMustStop', sender=item.file)

    def _AH_DownloadFile(self):
        item = self.selectedIndexes()[0].data(Qt.ItemDataRole.UserRole)
        model = self.model()
        if item.downloading:
            return

        if item.expired:
            return True

        if 'downloads/%s' % item.id in item.filename:
            folder = "%s/%s" % (ApplicationData.get('downloads'), item.id)
            temporary_download_file = "%s.download" % item.filename
            if not os.path.exists(temporary_download_file):
                try:
                    shutil.rmtree(folder)
                except FileNotFoundError:
                    pass
                else:
                    message_log.info(f'File transfer {item.id} removed from {folder}')

        if item.hash and item.file.protocol == 'msrp':
            SessionManager().get_file(model.session.contact, model.session.contact_uri, item.filename, item.hash, item.id, account=item.account)
        else:
            SessionManager().get_file_from_url(model.session, item.file)

    def _AH_RemoveFile(self):
        item = self.selectedIndexes()[0].data(Qt.ItemDataRole.UserRole)
        model = self.model()
        self.model().deleteItem(item)

    def _AH_OpenTransfersFolder(self):
        item = self.selectedIndexes()[0].data(Qt.ItemDataRole.UserRole)
        if item.hash and item.file.protocol == 'msrp':
            settings = BlinkSettings()
            QDesktopServices.openUrl(QUrl.fromLocalFile(settings.transfers_directory.normalized))
            return

        directory = os.path.dirname(item.filename)
        QDesktopServices.openUrl(QUrl.fromLocalFile(directory))


# Conference participants
#

class ConferenceParticipantWidget(ChatSessionWidget):
    def update_content(self, participant):
        self.setDisabled(participant.pending_request)
        self.name_label.setText(participant.name)
        self.info_label.setText(participant.info)
        self.icon_label.setPixmap(participant.pixmap)
        self.state_label.state = participant.state
        self.hold_icon.setVisible(participant.on_hold)
        self.composing_icon.setVisible(participant.is_composing)
        self.chat_icon.setVisible('chat' in participant.active_media)
        self.audio_icon.setVisible('audio' in participant.active_media)
        self.video_icon.setVisible('video' in participant.active_media)
        self.screen_sharing_icon.setVisible('screen-sharing' in participant.active_media)


@implementer(IObserver)
class ConferenceParticipantItem(object):

    size_hint = QSize(200, 36)

    def __init__(self, participant):
        self.participant = participant
        self.widget = ConferenceParticipantWidget(None)
        self.widget.update_content(self)
        notification_center = NotificationCenter()
        notification_center.add_observer(ObserverWeakrefProxy(self), sender=participant)

    def __repr__(self):
        return '%s(%r)' % (self.__class__.__name__, self.participant)

    @property
    def pending_request(self):
        return self.participant.pending_request

    @property
    def name(self):
        if self.participant.contact.type == 'dummy':
            return self.participant.display_name or self.participant.contact.name
        else:
            return self.participant.contact.name

    @property
    def info(self):
        return self.participant.request_status or self.participant.contact.note or self.participant.uri

    @property
    def state(self):
        return self.participant.contact.state

    @property
    def on_hold(self):
        return self.participant.on_hold

    @property
    def is_composing(self):
        return self.participant.is_composing

    @property
    def active_media(self):
        return self.participant.active_media

    @property
    def icon(self):
        return self.participant.contact.icon

    @property
    def pixmap(self):
        return self.participant.contact.pixmap

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_ConferenceParticipantDidChange(self, notification):
        self.widget.update_content(self)
        notification.center.post_notification('ConferenceParticipantItemDidChange', sender=self)


class ConferenceParticipantDelegate(QStyledItemDelegate, ColorHelperMixin):
    def __init__(self, parent=None):
        super(ConferenceParticipantDelegate, self).__init__(parent)

    def editorEvent(self, event, model, option, index):
        if event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton and event.modifiers() == Qt.KeyboardModifier.NoModifier:
            cross_rect = option.rect.adjusted(int(option.rect.width()) - 14, 0, 0, int(-option.rect.height() / 2))  # top half of the rightmost 14 pixels
            if cross_rect.contains(event.pos()):
                item = index.data(Qt.ItemDataRole.UserRole)
                model.session.server_conference.remove_participant(item.participant)
                return True
        return super(ConferenceParticipantDelegate, self).editorEvent(event, model, option, index)

    def paint(self, painter, option, index):
        participant = index.data(Qt.ItemDataRole.UserRole)
        if option.state & QStyle.StateFlag.State_Selected:
            participant.widget.display_mode = participant.widget.SelectedDisplayMode
        elif index.row() % 2 == 0:
            participant.widget.display_mode = participant.widget.StandardDisplayMode
        else:
            participant.widget.display_mode = participant.widget.AlternateDisplayMode
        participant.widget.setFixedSize(option.rect.size())

        painter.save()
        painter.drawPixmap(option.rect, participant.widget.grab())
        if (option.state & QStyle.StateFlag.State_MouseOver) and participant.widget.isEnabled():
            self.drawRemoveIndicator(participant, option, painter, participant.widget)
        if 0 and (option.state & QStyle.StateFlag.State_MouseOver):
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            if option.state & QStyle.StateFlag.State_Selected:
                painter.fillRect(option.rect, QColor(240, 244, 255, 40))
            else:
                painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
                painter.fillRect(option.rect, QColor(240, 244, 255, 230))
        painter.restore()

    def drawRemoveIndicator(self, participant, option, painter, widget):
        pen_thickness = 1.6

        if widget.state_label.state is not None:
            foreground_color = option.palette.color(QPalette.ColorGroup.Normal, QPalette.ColorRole.WindowText)
            background_color = widget.state_label.state_colors[widget.state_label.state]
            base_contrast_color = self.calc_light_color(background_color)
            gradient = QLinearGradient(0, 0, 1, 0)
            gradient.setCoordinateMode(QLinearGradient.CoordinateMode.ObjectBoundingMode)
            gradient.setColorAt(0.0, self.color_with_alpha(base_contrast_color, 0.3 * 255))
            gradient.setColorAt(1.0, self.color_with_alpha(base_contrast_color, 0.8 * 255))
            contrast_color = QBrush(gradient)
        else:
            foreground_color = widget.palette().color(QPalette.ColorGroup.Normal, widget.foregroundRole())
            background_color = widget.palette().color(widget.backgroundRole())
            contrast_color = self.calc_light_color(background_color)
        line_color = self.deco_color(background_color, foreground_color)

        pen = QPen(line_color, pen_thickness, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        contrast_pen = QPen(contrast_color, pen_thickness, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)

        # draw the remove indicator at the top (works best with a state_label of width 14)
        cross_rect = QRect(0, 0, 14, 14)
        cross_rect.moveTopRight(widget.state_label.geometry().topRight())
        cross_rect.translate(option.rect.topLeft())

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        painter.translate(cross_rect.center())
        painter.translate(+1.5, +1)
        painter.translate(0, +1)
        painter.setPen(contrast_pen)
        painter.drawLine(-3, -3, 3, 3)
        painter.drawLine(-3, 3, 3, -3)
        painter.translate(0, -1)
        painter.setPen(pen)
        painter.drawLine(-3, -3, 3, 3)
        painter.drawLine(-3, 3, 3, -3)
        painter.restore()

    def sizeHint(self, option, index):
        return index.data(Qt.ItemDataRole.SizeHintRole)


@implementer(IObserver)
class ConferenceParticipantModel(QAbstractListModel):

    participantAboutToBeAdded = pyqtSignal(ConferenceParticipantItem)
    participantAboutToBeRemoved = pyqtSignal(ConferenceParticipantItem)
    participantAdded = pyqtSignal(ConferenceParticipantItem)
    participantRemoved = pyqtSignal(ConferenceParticipantItem)

    # The MIME types we accept in drop operations, in the order they should be handled
    accepted_mime_types = ['application/x-blink-contact-list', 'application/x-blink-contact-uri-list', 'text/uri-list']

    def __init__(self, session, parent=None):
        super(ConferenceParticipantModel, self).__init__(parent)
        self.session = session
        self.participants = []

        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=session)

    def flags(self, index):
        if index.isValid():
            return QAbstractListModel.flags(self, index) | Qt.ItemFlag.ItemIsDropEnabled
        else:
            return QAbstractListModel.flags(self, index) | Qt.ItemFlag.ItemIsDropEnabled

    def rowCount(self, parent=QModelIndex()):
        return len(self.participants)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        item = self.participants[index.row()]
        if role == Qt.ItemDataRole.UserRole:
            return item
        elif role == Qt.ItemDataRole.SizeHintRole:
            return item.size_hint
        elif role == Qt.ItemDataRole.DisplayRole:
            return str(item)
        return None

    def supportedDropActions(self):
        return Qt.DropAction.CopyAction  # | Qt.DropAction.MoveAction

    def dropMimeData(self, mime_data, action, row, column, parent_index):
        # this is here just to keep the default Qt DnD API happy
        # the custom handler is in handleDroppedData
        return False

    def handleDroppedData(self, mime_data, action, index):
        if action == Qt.DropAction.IgnoreAction:
            return True

        for mime_type in self.accepted_mime_types:
            if mime_data.hasFormat(mime_type):
                name = mime_type.replace('/', ' ').replace('-', ' ').title().replace(' ', '')
                handler = getattr(self, '_DH_%s' % name)
                return handler(mime_data, action, index)
        else:
            return False

    def _DH_ApplicationXBlinkContactList(self, mime_data, action, index):
        try:
            contacts = pickle.loads(str(mime_data.data('application/x-blink-contact-list')))
        except Exception:
            return False
        for contact in contacts:
            self.session.server_conference.add_participant(contact, contact.uri)
        return True

    def _DH_ApplicationXBlinkContactUriList(self, mime_data, action, index):
        try:
            contact, contact_uris = pickle.loads(str(mime_data.data('application/x-blink-contact-uri-list')))
        except Exception:
            return False
        for contact_uri in contact_uris:
            self.session.server_conference.add_participant(contact, contact_uri.uri)
        return True

    def _DH_TextUriList(self, mime_data, action, index):
        return False

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_BlinkSessionDidEnd(self, notification):
        self.clear()

    def _NH_BlinkSessionWasDeleted(self, notification):
        notification.center.remove_observer(self, sender=self.session)
        self.session = None

    def _NH_BlinkSessionWillAddParticipant(self, notification):
        self.addParticipant(ConferenceParticipantItem(notification.data.participant))

    def _NH_BlinkSessionDidNotAddParticipant(self, notification):
        self.removeParticipant(notification.data.participant.participant_item)

    def _NH_BlinkSessionDidRemoveParticipant(self, notification):
        self.removeParticipant(notification.data.participant.participant_item)

    def _NH_ConferenceParticipantItemDidChange(self, notification):
        index = self.index(self.participants.index(notification.sender))
        self.dataChanged.emit(index, index)

    def _find_insertion_point(self, participant):
        for position, item in enumerate(self.participants):
            if item.name > participant.name:
                break
        else:
            position = len(self.participants)
        return position

    def _add_participant(self, participant):
        position = self._find_insertion_point(participant)
        self.beginInsertRows(QModelIndex(), position, position)
        self.participants.insert(position, participant)
        self.endInsertRows()

    def _pop_participant(self, participant):
        position = self.participants.index(participant)
        self.beginRemoveRows(QModelIndex(), position, position)
        del self.participants[position]
        self.endRemoveRows()
        return participant

    def addParticipant(self, participant):
        if participant in self.participants:
            return
        participant.participant.participant_item = participant  # add a back reference to this item so we can find it later without iterating all participants
        self.participantAboutToBeAdded.emit(participant)
        self._add_participant(participant)
        self.participantAdded.emit(participant)
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=participant)

    def removeParticipant(self, participant):
        if participant not in self.participants:
            return
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=participant)
        del participant.participant.participant_item
        self.participantAboutToBeRemoved.emit(participant)
        self._pop_participant(participant)
        self.participantRemoved.emit(participant)

    def clear(self):
        notification_center = NotificationCenter()
        self.beginResetModel()
        for participant in self.participants:
            del participant.participant.participant_item
            notification_center.remove_observer(self, sender=participant)
        self.participants = []
        self.endResetModel()


class ConferenceParticipantListView(QListView, ColorHelperMixin):
    def __init__(self, parent=None):
        super(ConferenceParticipantListView, self).__init__(parent)
        self.setItemDelegate(ConferenceParticipantDelegate(self))
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.context_menu = QMenu(self)
        self.actions = ContextMenuActions()
        self.paint_drop_indicator = False

    def setModel(self, model):
        selection_model = self.selectionModel()
        if selection_model is not None:
            selection_model.deleteLater()
        super(ConferenceParticipantListView, self).setModel(model)

    def contextMenuEvent(self, event):
        pass

    def hideEvent(self, event):
        self.context_menu.hide()

    def paintEvent(self, event):
        super(ConferenceParticipantListView, self).paintEvent(event)
        if self.paint_drop_indicator:
            rect = self.viewport().rect()  # or should this be self.contentsRect() ? -Dan
            # color = QColor('#b91959')
            # color = QColor('#00aaff')
            # color = QColor('#55aaff')
            # color = QColor('#00aa00')
            # color = QColor('#aa007f')
            # color = QColor('#dd44aa')
            color = QColor('#aa007f')
            pen_color = self.color_with_alpha(color, 120)
            brush_color = self.color_with_alpha(color, 10)
            painter = QPainter(self.viewport())
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setBrush(brush_color)
            painter.setPen(QPen(pen_color, 1.6))
            painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 3, 3)
            painter.end()

    def dragEnterEvent(self, event):
        model = self.model()
        accepted_mime_types = set(model.accepted_mime_types)
        provided_mime_types = set(event.mimeData().formats())
        acceptable_mime_types = accepted_mime_types & provided_mime_types
        if not acceptable_mime_types:
            event.ignore()
        else:
            event.accept()

    def dragLeaveEvent(self, event):
        super(ConferenceParticipantListView, self).dragLeaveEvent(event)
        self.paint_drop_indicator = False
        self.viewport().update()

    def dragMoveEvent(self, event):
        super(ConferenceParticipantListView, self).dragMoveEvent(event)

        model = self.model()
        mime_data = event.mimeData()

        for mime_type in model.accepted_mime_types:
            if mime_data.hasFormat(mime_type):
                handler = getattr(self, '_DH_%s' % mime_type.replace('/', ' ').replace('-', ' ').title().replace(' ', ''))
                handler(event)
                self.viewport().update()
                break
        else:
            event.ignore()

    def dropEvent(self, event):
        model = self.model()
        if model.handleDroppedData(event.mimeData(), event.dropAction(), self.indexAt(event.pos())):
            event.accept()
        super(ConferenceParticipantListView, self).dropEvent(event)
        self.paint_drop_indicator = False
        self.viewport().update()

    def _DH_ApplicationXBlinkContactList(self, event):
        event.accept(self.viewport().rect())
        self.paint_drop_indicator = True

    def _DH_ApplicationXBlinkContactUriList(self, event):
        event.accept(self.viewport().rect())
        self.paint_drop_indicator = True

    def _DH_TextUriList(self, event):
        event.ignore(self.viewport().rect())
        # event.accept(self.viewport().rect())
        # self.paint_drop_indicator = True


# Session management
#

class DialogSlots(object):
    def __init__(self, iterable):
        self.all = set(iterable)
        self.used = set()

    def reserve(self):
        try:
            slot = min(self.all - self.used)
        except ValueError:
            return None
        else:
            self.used.add(slot)
            return slot

    def release(self, slot):
        if slot is not None:
            self.used.remove(slot)


class IncomingDialogBase(QDialog):
    _slots = DialogSlots(list(range(1, 100)))

    slot = None

    def showEvent(self, event):
        if not event.spontaneous():
            self.slot = slot = self._slots.reserve()

            blink = QApplication.instance()
            # We can also display on the main window screen if desired ... -- Tijmen
            # screen = blink.screenAt(blink.main_window.pos())
            screen = blink.primaryScreen()
            screen_geometry = screen.geometry()
            available_geometry = screen.availableGeometry()
            window_frame_size = blink.main_window.frameSize() - blink.main_window.size()

            width = limit(self.sizeHint().width(), min=self.minimumSize().width(), max=min(self.maximumSize().width(), available_geometry.width() - window_frame_size.width()))
            height = limit(self.sizeHint().height(), min=self.minimumSize().height(), max=min(self.maximumSize().height(), available_geometry.height() - window_frame_size.height()))
            total_width = width + window_frame_size.width()
            total_height = height + window_frame_size.height()

            x = int(limit(screen_geometry.center().x() - total_width / 2, min=available_geometry.left(), max=available_geometry.right() - total_width))
            if slot is None:
                y = -1
            elif slot % 2 == 0:
                y = int(screen_geometry.center().y() + (slot-1) * total_height / 2)
            else:
                y = int(screen_geometry.center().y() - slot * total_height / 2)

            if available_geometry.top() <= y <= available_geometry.bottom() - total_height:
                self.setGeometry(x, y, width, height)
            else:
                self.resize(width, height)

    def hideEvent(self, event):
        if not event.spontaneous():
            self._slots.release(self.slot)
            self.slot = None


ui_class, base_class = uic.loadUiType(Resources.get('incoming_dialog.ui'))


class IncomingDialog(IncomingDialogBase, ui_class):
    def __init__(self, parent=None):
        super(IncomingDialog, self).__init__(parent)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        with Resources.directory:
            self.setupUi(self)
        default_font_size = self.uri_label.fontInfo().pointSizeF()
        name_font_size = limit(default_font_size + 3, max=14)
        note_font_size = limit(default_font_size - 1, max=9)
        font = self.username_label.font()
        font.setPointSizeF(name_font_size)
        self.username_label.setFont(font)
        font = self.note_label.font()
        font.setPointSizeF(note_font_size)
        self.note_label.setFont(font)
        self.reject_mode = 'ignore'
        self.slot = None
        for stream in self.streams:
            stream.toggled.connect(self._update_accept_button)
            stream.hidden.connect(self._update_streams_layout)
            stream.shown.connect(self._update_streams_layout)
        self.busy_button.released.connect(self._set_busy_mode)
        self.reject_button.released.connect(self._set_reject_mode)
        self.screensharing_stream.hidden.connect(self.screensharing_label.hide)
        self.screensharing_stream.shown.connect(self.screensharing_label.show)
        self.auto_answer_label.setText(translate('incoming_dialog', 'Auto-answer is inactive'))
        self.auto_answer_interval = None
        self._auto_answer_timer = None
        self.passed_time = 0
        self.auto_answer_confirmed = False

    def show(self, activate=True):
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, not activate)
        super(IncomingDialog, self).show()

    @property
    def streams(self):
        return self.audio_stream, self.chat_stream, self.screensharing_stream, self.video_stream

    @property
    def accepted_streams(self):
        return [stream for stream in self.streams if stream.in_use and stream.accepted]

    def _set_busy_mode(self):
        self.reject_mode = 'busy'

    def _set_reject_mode(self):
        self.reject_mode = 'reject'

    def _update_accept_button(self):
        was_enabled = self.accept_button.isEnabled()
        self.accept_button.setEnabled(len(self.accepted_streams) > 0)
        if self.accept_button.isEnabled() != was_enabled:
            self.accept_button.setFocus()

    def setAutoAnswer(self, interval):
        self.auto_answer_interval = interval
        self._auto_answer_timer = QTimer()
        self._auto_answer_timer.setInterval(1000)
        self._auto_answer_timer.timeout.connect(self._update_auto_answer)
        self._auto_answer_timer.start()
        self.auto_answer_label.setText(translate('incoming_dialog', 'Auto answer in %d seconds') % interval)

    def _update_auto_answer(self):
        self.passed_time = self.passed_time + 1
        remaining_time = self.auto_answer_interval - self.passed_time
        self.auto_answer_label.setText(translate('incoming_dialog', 'Auto answer in %d seconds') % remaining_time) 
        if remaining_time == 0:
            self._auto_answer_timer.stop()
            self.hide()

    def _update_streams_layout(self):
        if len([stream for stream in self.streams if stream.in_use]) > 1:
            self.audio_stream.active = True
            self.chat_stream.active = True
            self.screensharing_stream.active = True
            self.video_stream.active = True
            self.note_label.setText(translate('incoming_dialog', 'To refuse a media type click its icon'))
        else:
            self.audio_stream.active = False
            self.chat_stream.active = False
            self.screensharing_stream.active = False
            self.video_stream.active = False
            if self.audio_stream.in_use:
                self.note_label.setText(translate('incoming_dialog', 'Audio call'))
            elif self.chat_stream.in_use:
                self.note_label.setText(translate('incoming_dialog', 'Chat session'))
            elif self.video_stream.in_use:
                self.note_label.setText(translate('incoming_dialog', 'Video call'))
            elif self.screensharing_stream.in_use:
                self.note_label.setText(translate('incoming_dialog', 'Screen sharing request'))
            else:
                self.note_label.setText('')
        self._update_accept_button()


del ui_class, base_class


class IncomingRequest(QObject):
    finished = pyqtSignal(object)
    accepted = pyqtSignal(object)
    rejected = pyqtSignal(object, str)

    def __init__(self, dialog, session, contact, contact_uri, proposal=False, audio_stream=None, video_stream=None, chat_stream=None, screensharing_stream=None):
        super(IncomingRequest, self).__init__()
        self.dialog = dialog
        self.session = session
        self.contact = contact
        self.contact_uri = contact_uri
        self.proposal = proposal
        self.audio_stream = audio_stream
        self.video_stream = video_stream
        self.chat_stream = chat_stream
        self.screensharing_stream = screensharing_stream
        self._auto_answer_timer = None

        if proposal:
            self.dialog.setWindowTitle(translate('incoming_dialog', 'Incoming Session Update'))
            self.dialog.busy_button.hide()
        else:
            self.dialog.setWindowTitle(translate('incoming_dialog', 'Incoming Session Request'))
        address = '%s@%s' % (session.remote_identity.uri.user, session.remote_identity.uri.host)
        self.dialog.uri_label.setText(address)
        self.dialog.username_label.setText(contact.name or session.remote_identity.display_name or address)
        settings = SIPSimpleSettings()

        auto_answer_interval = 0

        if settings.sip.auto_answer and settings.sip.auto_answer_interval and session.account.sip.auto_answer:
            if session.account is BonjourAccount():
                auto_answer_interval = settings.sip.auto_answer_interval
            else:
                if hasattr(contact.settings, 'auto_answer'):
                    if contact.settings.auto_answer:
                        auto_answer_interval = settings.sip.auto_answer_interval

        if auto_answer_interval > 0:
            self.dialog.setAutoAnswer(auto_answer_interval)
            self._auto_answer_timer = QTimer()
            self._auto_answer_timer.setInterval(auto_answer_interval * 1000)
            self._auto_answer_timer.setSingleShot(True)
            self._auto_answer_timer.timeout.connect(self._auto_answer)
            self._auto_answer_timer.start()

        self.dialog.user_icon.setPixmap(contact.icon.pixmap(48))
        self.dialog.audio_stream.setVisible(self.audio_stream is not None)
        self.dialog.video_stream.setVisible(self.video_stream is not None)
        self.dialog.chat_stream.setVisible(self.chat_stream is not None)
        self.dialog.screensharing_stream.setVisible(self.screensharing_stream is not None)
        if self.screensharing_stream is not None:
            if self.screensharing_stream.handler.type == 'active':
                self.dialog.screensharing_label.setText(translate('incoming_dialog', 'is offering screen sharing'))
            else:
                self.dialog.screensharing_label.setText(translate('incoming_dialog', 'is asking to share your screen'))
                # self.dialog.screensharing_stream.accepted = bool(proposal)


        self.dialog.finished.connect(self._SH_DialogFinished)

    def __eq__(self, other):
        return self is other

    def __ne__(self, other):
        return self is not other

    def __lt__(self, other):
        return self.priority < other.priority

    def __le__(self, other):
        return self.priority <= other.priority

    def __gt__(self, other):
        return self.priority > other.priority

    def __ge__(self, other):
        return self.priority >= other.priority

    @property
    def accepted_streams(self):
        streams = []
        if self.audio_accepted:
            streams.append(self.audio_stream)
        if self.video_accepted:
            streams.append(self.video_stream)
        if self.chat_accepted:
            streams.append(self.chat_stream)
        if self.screensharing_accepted:
            streams.append(self.screensharing_stream)
        return streams

    @property
    def audio_accepted(self):
        return self.dialog.audio_stream.in_use and self.dialog.audio_stream.accepted

    @property
    def video_accepted(self):
        return self.dialog.video_stream.in_use and self.dialog.video_stream.accepted

    @property
    def chat_accepted(self):
        return self.dialog.chat_stream.in_use and self.dialog.chat_stream.accepted

    @property
    def screensharing_accepted(self):
        return self.dialog.screensharing_stream.in_use and self.dialog.screensharing_stream.accepted

    @property
    def priority(self):
        if self.audio_stream:
            return 0
        elif self.video_stream:
            return 1
        elif self.screensharing_stream:
            return 2
        elif self.chat_stream:
            return 3
        else:
            return sys.maxsize

    @property
    def stream_types(self):
        return {stream.type for stream in (self.audio_stream, self.video_stream, self.screensharing_stream, self.chat_stream) if stream is not None}

    def _auto_answer(self):
        self._SH_DialogFinished(QDialog.DialogCode.Accepted)

    def _SH_DialogFinished(self, result):
        if self._auto_answer_timer and self._auto_answer_timer.isActive():
            self._auto_answer_timer.stop()

        self.finished.emit(self)
        if result == QDialog.DialogCode.Accepted:
            self.accepted.emit(self)
        elif result == QDialog.DialogCode.Rejected:
            self.rejected.emit(self, self.dialog.reject_mode)


ui_class, base_class = uic.loadUiType(Resources.get('incoming_filetransfer_dialog.ui'))


class IncomingFileTransferDialog(IncomingDialogBase, ui_class):
    def __init__(self, parent=None):
        super(IncomingFileTransferDialog, self).__init__(parent)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        with Resources.directory:
            self.setupUi(self)
        default_font_size = self.uri_label.fontInfo().pointSizeF()
        name_font_size = limit(default_font_size + 3, max=14)
        font = self.username_label.font()
        font.setPointSizeF(name_font_size)
        self.username_label.setFont(font)
        self.slot = None
        self.reject_mode = 'ignore'
        self.reject_button.released.connect(self._set_reject_mode)

    def show(self, activate=True):
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, not activate)
        super(IncomingFileTransferDialog, self).show()

    def _set_reject_mode(self):
        self.reject_mode = 'reject'


del ui_class, base_class


class IncomingFileTransferRequest(QObject):
    finished = pyqtSignal(object)
    accepted = pyqtSignal(object)
    rejected = pyqtSignal(object, str)

    priority = 4
    stream_types = {'file-transfer'}

    def __init__(self, dialog, contact, contact_uri, session, stream):
        super(IncomingFileTransferRequest, self).__init__()
        self.dialog = dialog
        self.contact = contact
        self.contact_uri = contact_uri
        self.session = session
        self.stream = stream

        self.dialog.uri_label.setText(contact_uri.uri)
        self.dialog.username_label.setText(contact.name)
        self.dialog.user_icon.setPixmap(contact.icon.pixmap(48))
        filename = os.path.basename(self.stream.file_selector.name)
        size = self.stream.file_selector.size
        if size:
            self.dialog.file_label.setText(translate('incoming_filetransfer_dialog', 'File: %s (%s)') % (filename, FileSizeFormatter.format(size)))
        else:
            self.dialog.file_label.setText(translate('incoming_filetransfer_dialog', 'File: %s') % filename)

        self.dialog.finished.connect(self._SH_DialogFinished)

    def __eq__(self, other):
        return self is other

    def __ne__(self, other):
        return self is not other

    def __lt__(self, other):
        return self.priority < other.priority

    def __le__(self, other):
        return self.priority <= other.priority

    def __gt__(self, other):
        return self.priority > other.priority

    def __ge__(self, other):
        return self.priority >= other.priority

    def _SH_DialogFinished(self, result):
        self.finished.emit(self)
        if result == QDialog.DialogCode.Accepted:
            self.accepted.emit(self)
        elif result == QDialog.DialogCode.Rejected:
            self.rejected.emit(self, self.dialog.reject_mode)


ui_class, base_class = uic.loadUiType(Resources.get('incoming_calltransfer_dialog.ui'))


class IncomingCallTransferDialog(IncomingDialogBase, ui_class):
    def __init__(self, parent=None):
        super(IncomingCallTransferDialog, self).__init__(parent)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        with Resources.directory:
            self.setupUi(self)
        default_font_size = self.uri_label.fontInfo().pointSizeF()
        name_font_size = limit(default_font_size + 3, max=14)
        font = self.username_label.font()
        font.setPointSizeF(name_font_size)
        self.username_label.setFont(font)
        self.slot = None
        self.reject_mode = 'reject'

    def show(self, activate=True):
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, not activate)
        super(IncomingCallTransferDialog, self).show()

del ui_class, base_class


class IncomingCallTransferRequest(QObject):
    finished = pyqtSignal(object)
    accepted = pyqtSignal(object)
    rejected = pyqtSignal(object, str)

    priority = 0
    stream_types = {'audio'}

    def __init__(self, dialog, contact, contact_uri, blink_session):
        super(IncomingCallTransferRequest, self).__init__()
        self.dialog = dialog
        self.contact = contact
        self.contact_uri = contact_uri
        self.session = blink_session.sip_session

        self.dialog.uri_label.setText(contact_uri.uri)
        self.dialog.username_label.setText(contact.name)
        self.dialog.user_icon.setPixmap(contact.icon.pixmap(48))
        self.dialog.transfer_label.setText(translate('incoming_calltransfer_dialog', 'transfer requested by {}').format(blink_session.contact.name or blink_session.contact_uri.uri))

        self.dialog.finished.connect(self._SH_DialogFinished)

    def __eq__(self, other):
        return self is other

    def __ne__(self, other):
        return self is not other

    def __lt__(self, other):
        return self.priority < other.priority

    def __le__(self, other):
        return self.priority <= other.priority

    def __gt__(self, other):
        return self.priority > other.priority

    def __ge__(self, other):
        return self.priority >= other.priority

    def _SH_DialogFinished(self, result):
        self.finished.emit(self)
        if result == QDialog.DialogCode.Accepted:
            self.accepted.emit(self)
        elif result == QDialog.DialogCode.Rejected:
            self.rejected.emit(self, self.dialog.reject_mode)


ui_class, base_class = uic.loadUiType(Resources.get('conference_dialog.ui'))


class ConferenceDialog(base_class, ui_class):
    def __init__(self, parent=None):
        super(ConferenceDialog, self).__init__(parent)
        with Resources.directory:
            self.setupUi(self)
        self.audio_button.clicked.connect(self._SH_MediaButtonClicked)
        self.chat_button.clicked.connect(self._SH_MediaButtonClicked)
        self.room_button.editTextChanged.connect(self._SH_RoomButtonEditTextChanged)
        self.accepted.connect(self.join_conference)
        geometry = QSettings().value("conference_dialog/geometry")
        if geometry:
            self.restoreGeometry(geometry)

    def _SH_MediaButtonClicked(self, checked):
        self.accept_button.setEnabled(any(button.isChecked() for button in (self.audio_button, self.chat_button)))

    def _SH_RoomButtonEditTextChanged(self, text):
        self.accept_button.setEnabled(any(button.isChecked() for button in (self.audio_button, self.chat_button)))

    def closeEvent(self, event):
        QSettings().setValue("conference_dialog/geometry", self.saveGeometry())
        super(ConferenceDialog, self).closeEvent(event)

    def show(self):
        geometry = QSettings().value("conference_dialog/geometry")
        if geometry:
            self.restoreGeometry(geometry)

        self.room_button.setCurrentIndex(-1)
        self.audio_button.setChecked(True)
        self.chat_button.setChecked(True)
        self.accept_button.setEnabled(True)
        self.raise_()
        super(ConferenceDialog, self).show()

    def join_conference(self):
        QSettings().setValue("conference_dialog/geometry", self.saveGeometry())
        from blink.contacts import URIUtils

        current_text = self.room_button.currentText()
        if self.room_button.findText(current_text) == -1:
            if self.room_button.count() == self.room_button.maxCount():
                self.room_button.removeItem(self.room_button.count()-1)
            self.room_button.insertItem(0, current_text)

        if not current_text:
            current_text = random.choice('123456789') + ''.join(random.choice('0123456789') for x in range(6))
        account_manager = AccountManager()
        session_manager = SessionManager()
        account = account_manager.default_account
        if account is not BonjourAccount():
            conference_uri = '%s@%s' % (current_text, account.server.conference_server or 'conference.%s' % account.id.domain) if '@' not in current_text else current_text
        else:
            conference_uri = '%s@%s' % (current_text, 'conference.sip2sip.info') if '@' not in current_text else current_text
        contact, contact_uri = URIUtils.find_contact(conference_uri, display_name='Conference')
        streams = []
        if self.audio_button.isChecked():
            streams.append(StreamDescription('audio'))
        if self.chat_button.isChecked():
            streams.append(StreamDescription('chat'))
        session_manager.create_session(contact, contact_uri, streams, account=account)


del ui_class, base_class


class RingtoneDescriptor(object):
    def __init__(self):
        self.values = weakobjectmap()

    def __get__(self, instance, owner):
        if instance is None:
            return self
        return self.values[instance]

    def __set__(self, obj, ringtone):
        old_ringtone = self.values.get(obj, Null)
        if ringtone is not Null and ringtone.type == old_ringtone.type:
            return
        old_ringtone.stop()
        old_ringtone.bridge.remove(old_ringtone)
        ringtone.bridge.add(ringtone)
        ringtone.start()
        self.values[obj] = ringtone

    def __delete__(self, obj):
        raise AttributeError("Attribute cannot be deleted")


class RequestList(list):
    def __getitem__(self, key):
        if isinstance(key, int):
            return super(RequestList, self).__getitem__(key)
        elif isinstance(key, tuple):
            session, item_type = key
            return [item for item in self if item.session is session and isinstance(item, item_type)]
        else:
            return [item for item in self if item.session is key]


@implementer(IObserver)
class SessionManager(object, metaclass=Singleton):

    class PrimaryRingtone(metaclass=MarkerType):   pass
    class SecondaryRingtone(metaclass=MarkerType): pass

    inbound_ringtone  = RingtoneDescriptor()
    outbound_ringtone = RingtoneDescriptor()
    hold_tone         = RingtoneDescriptor()

    def __init__(self):
        self.sessions = []
        self.file_transfers = []
        self.incoming_requests = RequestList()
        self.last_dialed_uri = None
        self.send_file_directory = Path('~').normalized
        self.active_session = None
        self.must_cancel_downloads = set()

        self.inbound_ringtone = Null
        self.outbound_ringtone = Null
        self.hold_tone = Null

        self._hangup_tone_timer = QTimer()
        self._hangup_tone_timer.setInterval(1000)
        self._hangup_tone_timer.setSingleShot(True)

        self._filetransfer_tone_timer = QTimer()
        self._filetransfer_tone_timer.setInterval(1500)
        self._filetransfer_tone_timer.setSingleShot(True)

        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='SIPSessionNewOutgoing')
        notification_center.add_observer(self, name='SIPSessionNewIncoming')
        notification_center.add_observer(self, name='SIPSessionDidFail')

        notification_center.add_observer(self, name='BlinkSessionWasCreated')
        notification_center.add_observer(self, name='BlinkFileTransferWasCreated')
        notification_center.add_observer(self, name='BlinkFileTransferWillRetry')
        notification_center.add_observer(self, name='BlinkHTTPTransferMustStop')
        notification_center.add_observer(self, name='BlinkSessionListSelectionChanged')

    def create_session(self, contact, contact_uri, streams, account=None, connect=True, sibling=None, remote_instance_id=None):
        if account is None:
            if contact.type == 'bonjour':
                account = BonjourAccount()
            else:
                account = AccountManager().default_account

        try:
            session = next(session for session in self.sessions if session.reusable and session.contact.settings is contact.settings)
            reinitialize = True
        except StopIteration:
            session = BlinkSession()
            reinitialize = False

        session.init_outgoing(account, contact, contact_uri, streams, sibling=sibling, reinitialize=reinitialize, remote_instance_id=remote_instance_id)
        self.last_dialed_uri = session.uri
        if connect:
            session.connect()

        return session

    def send_file(self, contact, contact_uri, filename, transfer_id=RandomID, account=None):
        if account is None:
            if contact.type == 'bonjour':
                account = BonjourAccount()
            else:
                account = AccountManager().default_account

        self.send_file_directory = os.path.dirname(filename)

        transfer = BlinkFileTransfer()
        transfer.init_outgoing(account, contact, contact_uri, filename, transfer_id)
        transfer.connect()
        return transfer

    def get_file(self, contact, contact_uri, filename, hash, transfer_id=RandomID, account=None, conference_file=True, must_open=False):
        if account is None:
            if contact.type == 'bonjour':
                account = BonjourAccount()
            else:
                account = AccountManager().default_account

        self.send_file_directory = os.path.dirname(filename)

        if os.path.exists(filename):
            message_log.info(f"File {transfer_id} already downloaded {filename}")
            return

        message_log.info(f"Geting file {transfer_id} to {filename}")

        transfer = BlinkFileTransfer()
        transfer.init_outgoing_pull(account, contact, contact_uri, filename, hash, transfer_id, conference_file, must_open=must_open)
        transfer.connect()
        return transfer

    def _NH_PGPFileDidDecrypt(self, notification):
        notification.center.post_notification('BlinkFileTransferDidDecrypt',
                                              sender=notification.sender,
                                              data=NotificationData(reason='File decrypted', filename=notification.data.filename, must_open=notification.data.must_open, id=notification.data.id))

    def _NH_BlinkFileTransferDidDecrypt(self, notification):
        if notification.data.must_open:
            QDesktopServices.openUrl(QUrl.fromLocalFile(notification.data.filename))

        data = notification.data
        data.error = None

        notification_center = NotificationCenter()
        notification_center.post_notification('BlinkFileTransferDidEnd', sender=notification.sender, data=data)

    def _NH_BlinkHTTPTransferMustStop(self, notification):
        self.must_cancel_downloads.add(notification.sender.id)

    def _NH_BlinkHTTPFileTransferDidEnd(self, notification):
        file = notification.data.file
        must_open = notification.data.must_open
        session = notification.sender

        if file.name.endswith('.asc'):
            stream = StreamDescription('messages')
            message_stream = stream.create_stream()
            message_stream.blink_session = session
            if session.account.sms.enable_pgp and session.account.sms.private_key is not None and os.path.exists(session.account.sms.private_key.normalized):
                message_stream.enable_pgp()
            message_stream.decrypt_file(file.name, session, must_open=must_open, id=file.id)
        else:
            data = notification.data
            data.error = None
            data.filename = file.name
            data.id = file.id
            notification_center = NotificationCenter()
            notification_center.post_notification('BlinkFileTransferDidEnd', sender=session, data=data)

    @run_in_thread('file-io')
    def get_file_from_url(self, session, file, must_open=False):
        notification_center = NotificationCenter()
        directory = ApplicationData.get(f'downloads/{file.id}')
        makedirs(directory)
        full_filepath = os.path.join(directory, file.name)
        file.name = full_filepath
        tmp_path = "%s.download" % full_filepath

        if os.path.exists(full_filepath):
            message_log.info(f"File {file.id} already downloaded {full_filepath}")
            notification_center.post_notification('BlinkHTTPFileTransferDidEnd', sender=session, data=NotificationData(file=file, must_open=must_open))
            return

        if os.path.exists(tmp_path):
            downloaded_size = os.path.getsize(tmp_path)
            message_log.info(f"Temporary file {file.id} of {downloaded_size} bytes saved at {tmp_path}")
            resume_header = {'Range': 'bytes=%d-' % downloaded_size}
        else:
            downloaded_size = 0
            resume_header = {}

        message_log.info(f"Downloading file {file.id} from {file.url} from {downloaded_size}")
        notification_center = NotificationCenter()

        try:
            r = requests.get(file.url, timeout=10, stream=True, headers=resume_header)
            r.raise_for_status()
        except (requests.ConnectionError, requests.Timeout) as e:
            message_log.warning(f'HTTP filetransfer connection error: {e}')
            notification_center.post_notification('BlinkHTTPTransferFailed', sender=file, data=NotificationData(reason='Connection timeout'))
        except requests.HTTPError as e:
            message_log.warning(f'HTTP filetransfer error {e}')
            notification_center.post_notification('BlinkHTTPTransferFailed', sender=file, data=NotificationData(reason='Error %s' % str(e)))
        except requests.RequestException as e:
            message_log.warning(f'HTTP filetransfer error {e}')
            notification_center.post_notification('BlinkHTTPTransferFailed', sender=file, data=NotificationData(reason='Error: %s' % str(e)))
        else:
            file_size = int(r.headers.get('Content-Length', 0))
            #print(r.headers)
            message_log.info('Downloading %d bytes (%d)' % (file_size, file.size))
            current_bytes = downloaded_size
            with open(tmp_path, 'ab+' if resume_header else 'wb+') as output_file:
                chunk_size = 1024*1024
                for chunk in r.iter_content(chunk_size=chunk_size):
                    if file.id in self.must_cancel_downloads:
                        message_log.info(f'Download {file.id} stopped by user')
                        break

                    current_bytes = current_bytes + len(chunk)
                    notification_center.post_notification('BlinkHTTPTransferProgress', sender=file, data=NotificationData(bytes=current_bytes, total_bytes=file.size))
                    if chunk:
                        output_file.write(chunk)

                if file.id in self.must_cancel_downloads:
                    r.close()
                    self.must_cancel_downloads.discard(file.id)
                    notification_center.post_notification('BlinkHTTPTransferFailed', sender=file, data=NotificationData(reason='User cancelled'))
                    return

            os.rename(tmp_path, full_filepath)

            message_log.info(f'File {file.id} downloaded {current_bytes} bytes saved to {file.name} size {os.path.getsize(full_filepath)}')
            notification_center.post_notification('BlinkHTTPTransferCompleted', sender=file)
            notification_center.post_notification('BlinkHTTPFileTransferDidEnd', sender=session, data=NotificationData(file=file, must_open=must_open))

    def update_ringtone(self):
        settings = SIPSimpleSettings()

        # Outgoing ringtone
        outgoing_sessions_or_proposals = [session for session in self.sessions if session.state == 'connecting/ringing' and session.direction == 'outgoing' or session.state == 'connected/sent_proposal']
        outgoing_file_transfers = [transfer for transfer in self.file_transfers if transfer.state == 'connecting/ringing' and transfer.direction == 'outgoing']
        if any(not session.on_hold for session in outgoing_sessions_or_proposals) or outgoing_file_transfers:
            outbound_ringtone = settings.sounds.outbound_ringtone
            if outbound_ringtone and not settings.audio.silent:
                if any('audio' in session.streams.proposed and not session.on_hold for session in outgoing_sessions_or_proposals):
                    ringtone_path = outbound_ringtone.path
                    ringtone_type = self.PrimaryRingtone
                else:
                    ringtone_path = Resources.get('sounds/beeping_ringtone.wav')
                    ringtone_type = self.SecondaryRingtone
                outbound_ringtone = WavePlayer(SIPApplication.voice_audio_mixer, ringtone_path, outbound_ringtone.volume, loop_count=0, pause_time=5)
                outbound_ringtone.bridge = SIPApplication.voice_audio_bridge
                outbound_ringtone.type = ringtone_type
            else:
                outbound_ringtone = Null
        else:
            outbound_ringtone = Null

        # Incoming ringtone
        if self.incoming_requests:
            try:
                request = next(req for req in self.incoming_requests if req.stream_types.intersection({'audio', 'video'}))
                ringtone_type = self.PrimaryRingtone
            except StopIteration:
                request = self.incoming_requests[0]
                ringtone_type = self.SecondaryRingtone

            if outbound_ringtone.type is self.PrimaryRingtone:
                ringtone_type = self.SecondaryRingtone
                initial_delay = 1  # have a small delay to avoid sounds overlapping
            elif self.active_session is not None and self.active_session.state == 'connected/*':
                ringtone_type = self.SecondaryRingtone
                initial_delay = 0
            else:
                initial_delay = 0

            sound_file = request.session.account.sounds.inbound_ringtone or settings.sounds.inbound_ringtone
            if sound_file and not settings.audio.silent:
                if ringtone_type is self.PrimaryRingtone:
                    ringtone_path = sound_file.path
                else:
                    ringtone_path = Resources.get('sounds/beeping_ringtone.wav')
                inbound_ringtone = WavePlayer(SIPApplication.alert_audio_mixer, ringtone_path, volume=sound_file.volume, loop_count=0, pause_time=3, initial_delay=initial_delay)
                inbound_ringtone.bridge = SIPApplication.alert_audio_bridge
                inbound_ringtone.type = ringtone_type
            else:
                inbound_ringtone = Null
        else:
            inbound_ringtone = Null

        # Hold tone
        connected_sessions = [session for session in self.sessions if session.state == 'connected/*']
        connected_on_hold_sessions = [session for session in connected_sessions if session.on_hold]
        if outbound_ringtone is Null and inbound_ringtone is Null and connected_sessions and not settings.audio.silent:
            if len(connected_sessions) == len(connected_on_hold_sessions):
                hold_tone = WavePlayer(SIPApplication.alert_audio_mixer, Resources.get('sounds/hold_tone.wav'), loop_count=0, volume=30, initial_delay=45, pause_time=45)
                hold_tone.bridge = SIPApplication.alert_audio_bridge
                hold_tone.type = None
            elif len(connected_on_hold_sessions) > 0:
                hold_tone = WavePlayer(SIPApplication.voice_audio_mixer, Resources.get('sounds/hold_tone.wav'), loop_count=0, volume=30, initial_delay=45, pause_time=45)
                hold_tone.bridge = SIPApplication.voice_audio_bridge
                hold_tone.type = None
            else:
                hold_tone = Null
        else:
            hold_tone = Null

        self.outbound_ringtone = outbound_ringtone
        self.inbound_ringtone = inbound_ringtone
        self.hold_tone = hold_tone

    def _process_remote_proposal(self, blink_session):
        sip_session = blink_session.sip_session
        proposed_streams = blink_session.streams.proposed

        if not proposed_streams or proposed_streams.types == {'file-transfer'}:
            sip_session.reject_proposal(488)
            return

        if proposed_streams.types == {'chat'}:
            blink_session.accept_proposal(list(proposed_streams))
            return

        sip_session.send_ring_indication()

        contact = blink_session.contact
        contact_uri = blink_session.contact_uri

        audio_stream = proposed_streams.get('audio')
        video_stream = proposed_streams.get('video')
        chat_stream = proposed_streams.get('chat')
        screensharing_stream = proposed_streams.get('screen-sharing')

        dialog = IncomingDialog()  # Build the dialog without a parent in order to be displayed on the current workspace on Linux.
        incoming_request = IncomingRequest(dialog, sip_session, contact, contact_uri, proposal=True, audio_stream=audio_stream, video_stream=video_stream, chat_stream=chat_stream, screensharing_stream=screensharing_stream)
        incoming_request.finished.connect(self._SH_IncomingRequestFinished)
        incoming_request.accepted.connect(self._SH_IncomingRequestAccepted)
        incoming_request.rejected.connect(self._SH_IncomingRequestRejected)

        bisect.insort_right(self.incoming_requests, incoming_request)
        incoming_request.dialog.show(activate=QApplication.activeWindow() is not None and self.incoming_requests.index(incoming_request) == 0)

    def _SH_IncomingRequestFinished(self, incoming_request):
        self.incoming_requests.remove(incoming_request)
        self.update_ringtone()

    def _SH_IncomingRequestAccepted(self, incoming_request):
        accepted_streams = incoming_request.accepted_streams
        if incoming_request.proposal:
            blink_session = next(session for session in self.sessions if session.sip_session is incoming_request.session)
            blink_session.accept_proposal(accepted_streams)
        else:
            try:
                blink_session = next(session for session in self.sessions if session.reusable and session.contact.settings is incoming_request.contact.settings)
                reinitialize = True
            except StopIteration:
                blink_session = BlinkSession()
                reinitialize = False
            blink_session.init_incoming(incoming_request.session, accepted_streams, incoming_request.contact, incoming_request.contact_uri, reinitialize=reinitialize)

    def _SH_IncomingRequestRejected(self, incoming_request, mode):
        if incoming_request.proposal:
            incoming_request.session.reject_proposal(488)
        elif mode == 'busy':
            incoming_request.session.reject(486)
        elif mode == 'reject':
            incoming_request.session.reject(603)

    def _SH_IncomingFileTransferRequestAccepted(self, incoming_request):
        transfer = BlinkFileTransfer()
        transfer.init_incoming(incoming_request.contact, incoming_request.contact_uri, incoming_request.session, incoming_request.stream)

    def _SH_IncomingFileTransferRequestRejected(self, incoming_request, mode):
        if mode == 'reject':
            incoming_request.session.reject(603)

    def _SH_IncomingCallTransferRequestAccepted(self, incoming_request):
        try:
            incoming_request.session.accept_transfer()
        except IllegalStateError:
            pass

    def _SH_IncomingCallTransferRequestRejected(self, incoming_request, mode):
        try:
            incoming_request.session.reject_transfer()
        except IllegalStateError:
            pass

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPSessionNewIncoming(self, notification):
        from blink.contacts import URIUtils

        session = notification.sender

        stream_map = defaultdict(list)
        for stream in notification.data.streams:
            stream_map[stream.type].append(stream)

        audio_streams = stream_map['audio']
        video_streams = stream_map['video']
        chat_streams = stream_map['chat']
        screensharing_streams = stream_map['screen-sharing']
        filetransfer_streams = [stream for stream in stream_map['file-transfer'] if stream.direction == 'recvonly']    # Only accept receiving files

        if not audio_streams and not video_streams and not chat_streams and not screensharing_streams and not filetransfer_streams:
            session.reject(488)
            return

        contact, contact_uri = URIUtils.find_contact(session.remote_identity.uri, display_name=session.remote_identity.display_name, exact=False)

        if filetransfer_streams and not (audio_streams or video_streams or chat_streams or screensharing_streams):
            dialog = IncomingFileTransferDialog()  # Build the dialog without a parent in order to be displayed on the current workspace on Linux.
            incoming_request = IncomingFileTransferRequest(dialog, contact, contact_uri, session, filetransfer_streams[0])
            incoming_request.finished.connect(self._SH_IncomingRequestFinished)
            incoming_request.accepted.connect(self._SH_IncomingFileTransferRequestAccepted)
            incoming_request.rejected.connect(self._SH_IncomingFileTransferRequestRejected)
        else:
            audio_stream = audio_streams[0] if audio_streams else None
            video_stream = video_streams[0] if video_streams else None
            chat_stream = chat_streams[0] if chat_streams else None
            screensharing_stream = screensharing_streams[0] if screensharing_streams else None

            settings = SIPSimpleSettings()

            if chat_stream and not (audio_stream or video_stream or screensharing_stream) and contact.type != 'dummy' and settings.chat.auto_accept:
                try:
                    blink_session = next(session for session in self.sessions if session.reusable and session.contact.settings is contact.settings)
                    reinitialize = True
                except StopIteration:
                    blink_session = BlinkSession()
                    reinitialize = False
                blink_session.init_incoming(session, [chat_stream], contact, contact_uri, reinitialize=reinitialize)
                return

            dialog = IncomingDialog()  # Build the dialog without a parent in order to be displayed on the current workspace on Linux.
            incoming_request = IncomingRequest(dialog, session, contact, contact_uri, proposal=False, audio_stream=audio_stream, video_stream=video_stream, chat_stream=chat_stream, screensharing_stream=screensharing_stream)
            incoming_request.finished.connect(self._SH_IncomingRequestFinished)
            incoming_request.accepted.connect(self._SH_IncomingRequestAccepted)
            incoming_request.rejected.connect(self._SH_IncomingRequestRejected)

        session.send_ring_indication()

        bisect.insort_right(self.incoming_requests, incoming_request)
        incoming_request.dialog.show(activate=QApplication.activeWindow() is not None and self.incoming_requests.index(incoming_request) == 0)
        self.update_ringtone()

    def _NH_SIPSessionNewOutgoing(self, notification):
        sip_session = notification.sender
        if sip_session.transfer_info is not None:
            from blink.contacts import URIUtils

            contact, contact_uri = URIUtils.find_contact(sip_session.remote_identity.uri)
            try:
                blink_session = next(session for session in self.sessions if session.reusable and session.contact.settings is contact.settings)
                reinitialize = True
            except StopIteration:
                blink_session = BlinkSession()
                reinitialize = False
            blink_session.init_transfer(sip_session, notification.data.streams, contact, contact_uri, reinitialize=reinitialize)

    def _NH_SIPSessionDidFail(self, notification):
        if notification.sender.direction == 'incoming':
            for incoming_request in self.incoming_requests[notification.sender]:
                incoming_request.dialog.hide()
                self.incoming_requests.remove(incoming_request)
            self.update_ringtone()

    def _NH_BlinkSessionWasCreated(self, notification):
        self.sessions.append(notification.sender)
        notification.center.add_observer(self, sender=notification.sender)

    def _NH_BlinkSessionDidChangeState(self, notification):
        new_state = notification.data.new_state
        if new_state == 'connected/received_proposal':
            self._process_remote_proposal(notification.sender)
        if new_state == 'connected':
            for request in self.incoming_requests[notification.sender.sip_session, IncomingRequest]:
                request.dialog.hide()
                self.incoming_requests.remove(request)
        elif new_state == 'ending':
            for request in self.incoming_requests[notification.sender.sip_session]:
                request.dialog.hide()
                self.incoming_requests.remove(request)
        if new_state in ('connecting/ringing', 'connecting/early_media', 'connected/*', 'ending'):
            self.update_ringtone()
        if new_state == 'ending':
            notification.sender._play_hangup_tone = notification.data.old_state in ('connecting/*', 'connected/*') and notification.sender.streams.types.intersection({'audio', 'video'})

    def _NH_BlinkSessionDidChangeHoldState(self, notification):
        settings = SIPSimpleSettings()
        if notification.sender is self.active_session and notification.data.originator == 'remote' and notification.data.remote_hold and not notification.data.local_hold and not settings.audio.silent:
            player = WavePlayer(SIPApplication.voice_audio_bridge.mixer, Resources.get('sounds/hold_tone.wav'), loop_count=1, volume=30)
            SIPApplication.voice_audio_bridge.add(player)
            player.start()
        self.update_ringtone()

    def _NH_BlinkSessionDidRemoveStream(self, notification):
        settings = SIPSimpleSettings()
        if notification.data.stream.type in ('audio', 'video') and not self._hangup_tone_timer.isActive() and not settings.audio.silent:
            self._hangup_tone_timer.start()
            player = WavePlayer(SIPApplication.voice_audio_bridge.mixer, Resources.get('sounds/hangup_tone.wav'), volume=30)
            SIPApplication.voice_audio_bridge.add(player)
            player.start()

    def _NH_BlinkSessionDidEnd(self, notification):
        settings = SIPSimpleSettings()
        if notification.sender._play_hangup_tone and not self._hangup_tone_timer.isActive() and not settings.audio.silent:
            self._hangup_tone_timer.start()
            player = WavePlayer(SIPApplication.voice_audio_bridge.mixer, Resources.get('sounds/hangup_tone.wav'), volume=30)
            SIPApplication.voice_audio_bridge.add(player)
            player.start()

    def _NH_BlinkSessionWasDeleted(self, notification):
        self.sessions.remove(notification.sender)
        notification.center.remove_observer(self, sender=notification.sender)

    def _NH_BlinkSessionTransferNewIncoming(self, notification):
        from blink.contacts import URIUtils

        contact, contact_uri = URIUtils.find_contact(notification.data.transfer_destination)

        dialog = IncomingCallTransferDialog()  # Build the dialog without a parent in order to be displayed on the current workspace on Linux.
        incoming_request = IncomingCallTransferRequest(dialog, contact, contact_uri, notification.sender)
        incoming_request.finished.connect(self._SH_IncomingRequestFinished)
        incoming_request.accepted.connect(self._SH_IncomingCallTransferRequestAccepted)
        incoming_request.rejected.connect(self._SH_IncomingCallTransferRequestRejected)

        bisect.insort_right(self.incoming_requests, incoming_request)
        incoming_request.dialog.show(activate=QApplication.activeWindow() is not None and self.incoming_requests.index(incoming_request) == 0)
        self.update_ringtone()

    def _NH_BlinkSessionTransferDidFail(self, notification):
        for request in self.incoming_requests[notification.sender.sip_session, IncomingCallTransferRequest]:
            request.dialog.hide()
            self.incoming_requests.remove(request)
        self.update_ringtone()

    def _NH_BlinkFileTransferWasCreated(self, notification):
        self.file_transfers.append(notification.sender)
        notification.center.add_observer(self, sender=notification.sender)

    def _NH_BlinkFileTransferWillRetry(self, notification):
        self.file_transfers.append(notification.sender)
        notification.center.add_observer(self, sender=notification.sender)

    def _NH_BlinkFileTransferDidChangeState(self, notification):
        new_state = notification.data.new_state
        if new_state in ('connecting/ringing', 'connected', 'ending'):
            self.update_ringtone()

    def _NH_BlinkFileTransferDidEnd(self, notification):
        settings = SIPSimpleSettings()
        try:
            self.file_transfers.remove(notification.sender)
        except ValueError:
            # this was an HTTP transfer
            pass
        else:
            notification.center.remove_observer(self, sender=notification.sender)
            if not notification.data.error and not self._filetransfer_tone_timer.isActive() and not settings.audio.silent:
                self._filetransfer_tone_timer.start()
                player = WavePlayer(SIPApplication.voice_audio_bridge.mixer, Resources.get('sounds/file_transfer.wav'), volume=30)
                SIPApplication.voice_audio_bridge.add(player)
                player.start()

    def _NH_BlinkSessionListSelectionChanged(self, notification):
        selected_session = notification.data.selected_session
        deselected_session = notification.data.deselected_session
        old_active_session = self.active_session

        if selected_session is self.active_session:  # both None or both the same session. nothing to do in either case.
            return
        elif selected_session is None and deselected_session is old_active_session is not None:
            self.active_session = None
            sessions = deselected_session.client_conference.sessions if deselected_session.client_conference is not None else [deselected_session]
            for session in sessions:
                session.active = False
            notification.center.post_notification('BlinkActiveSessionDidChange', sender=self, data=NotificationData(previous_active_session=old_active_session, active_session=None))
        elif selected_session is not None and selected_session.state in ('connecting/*', 'connected/*') and selected_session.streams.types.intersection({'audio', 'video'}):
            old_active_session = old_active_session or Null
            new_active_session = selected_session
            if old_active_session.client_conference is not None and old_active_session.client_conference is not new_active_session.client_conference:
                for session in old_active_session.client_conference.sessions:
                    session.active = False
            elif old_active_session.client_conference is None:
                old_active_session.active = False
            if new_active_session.client_conference is not None and new_active_session.client_conference is not old_active_session.client_conference:
                for session in new_active_session.client_conference.sessions:
                    session.active = True
            elif new_active_session.client_conference is None:
                new_active_session.active = True
            self.active_session = selected_session
            notification.center.post_notification('BlinkActiveSessionDidChange', sender=self, data=NotificationData(previous_active_session=old_active_session or None, active_session=selected_session))

