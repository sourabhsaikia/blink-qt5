
import os
import re
import urllib.parse
import sys

from PyQt5 import uic
from PyQt5.QtCore import Qt, QEvent, QRegularExpression, QUrl
from PyQt5.QtGui import QFont, QRegularExpressionValidator, QValidator
from PyQt5.QtWidgets import QActionGroup, QApplication, QButtonGroup, QFileDialog, QListView, QListWidgetItem, QMessageBox, QSpinBox, QStyle, QStyleOptionComboBox, QStyledItemDelegate

from application import log
from application.notification import IObserver, NotificationCenter
from application.python import Null, limit
from functools import partial
from gnutls.crypto import X509Certificate, X509PrivateKey
from gnutls.errors import GNUTLSError
from zope.interface import implementer

from sipsimple.account import AccountManager, BonjourAccount
from sipsimple.application import SIPApplication
from sipsimple.configuration import DefaultValue
from sipsimple.configuration.datatypes import H264Profile, MSRPRelayAddress, Path, PortRange, SIPProxyAddress, STUNServerAddress, STUNServerAddressList
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.threading import run_in_thread

from blink.accounts import AddAccountDialog
from blink.chatwindow import ChatMessageStyle, ChatStyleError, ChatMessage, ChatEvent, ChatSender, ChatJSInterface
from blink.configuration.datatypes import FileURL
from blink.configuration.settings import BlinkSettings
from blink.resources import ApplicationData, Resources
from blink.logging import LogManager
from blink.util import QSingleton, call_in_gui_thread, run_in_gui_thread, translate


__all__ = ['PreferencesWindow', 'AccountListView', 'SIPPortEditor']


class LanguageError(Exception): pass


class Language(object):
    filename_regex = re.compile(r'(\w+)_(\w+).*\.(\w+$)')
    mapping = {"default": "Automatic (Default)",
               "en": "English",
               "nl": "Nederlands",
               "ro": "Română"}

    def __init__(self, file):
        if file in ['default', 'en']:
            self.name = self.mapping[file]
            self.language_code = file
            return

        match = self.filename_regex.match(file)
        if match[3] != 'qm':
            raise LanguageError('Unsupported file')

        try:
            self.name = self.mapping[match[2]]
        except KeyError:
            raise LanguageError('Unsupported file')

        self.language_code = match[2]


# LineEdit and ComboBox validators
#
class IDDPrefixValidator(QRegularExpressionValidator):
    def __init__(self, parent=None):
        super(IDDPrefixValidator, self).__init__(QRegularExpression('[0-9+*#]+'), parent)

    def fixup(self, input):
        return super(IDDPrefixValidator, self).fixup(input or '+')


class PrefixValidator(QRegularExpressionValidator):
    def __init__(self, parent=None):
        super(PrefixValidator, self).__init__(QRegularExpression('(None|[0-9+*#]+)'), parent)

    def fixup(self, input):
        return super(PrefixValidator, self).fixup(input or 'None')


class HostnameValidator(QRegularExpressionValidator):
    def __init__(self, parent=None):
        super(HostnameValidator, self).__init__(QRegularExpression(r'^([\w\-_]+(\.[\w\-_]+)*)?$', QRegularExpression.PatternOption.CaseInsensitiveOption), parent)


class SIPAddressValidator(QRegularExpressionValidator):
    def __init__(self, parent=None):
        super(SIPAddressValidator, self).__init__(QRegularExpression(r'^([\w\-_+%]+@[\w\-_]+(\.[\w\-_]+)*)?$', QRegularExpression.PatternOption.CaseInsensitiveOption), parent)

    def fixup(self, input):
        if input and '@' not in input:
            preferences_window = self.parent()
            input += '@%s' % preferences_window.selected_account.id.domain
        return super(SIPAddressValidator, self).fixup(input)


class WebURLValidator(QRegularExpressionValidator):
    def __init__(self, parent=None):
        super(WebURLValidator, self).__init__(QRegularExpression(r'^(https?://[\w\-_]+(\.[\w\-_]+)*(:\d+)?(/.*)?)?$', QRegularExpression.PatternOption.CaseInsensitiveOption), parent)


class XCAPRootValidator(WebURLValidator):
    def fixup(self, input):
        url = urllib.parse.urlparse(input)
        if not (url.scheme and url.netloc):
            input = ''
        return super(XCAPRootValidator, self).fixup(input)

    def validate(self, input, pos):
        state, input, pos = super(XCAPRootValidator, self).validate(input, pos)
        if state == QValidator.State.Acceptable:
            if input.endswith(('?', ';', '&')):
                state = QValidator.State.Invalid
            else:
                url = urllib.parse.urlparse(input)
                if url.params or url.query or url.fragment:
                    state = QValidator.State.Invalid
                elif url.port is not None:
                    port = int(url.port)
                    if not (0 < port <= 65535):
                        state = QValidator.State.Invalid
        return state, input, pos


# Custom widgets used in preferences.ui
#
class SIPPortEditor(QSpinBox):
    def __init__(self, parent=None):
        super(SIPPortEditor, self).__init__(parent)
        self.setRange(0, 65535)
        self.sibling = Null  # if there is a sibling port, its value is invalid for this port

    def stepBy(self, steps):
        value = self.value()
        sibling_value = self.sibling.value()
        if value + steps == sibling_value != 0:
            steps += steps/abs(steps)  # add one more unit in the right direction
        if 0 < value + steps < 1024:
            if steps < 0:
                steps = -value
            else:
                steps = 1024 - value
        if value+steps == sibling_value != 0:
            steps += steps / abs(steps)  # add one more unit in the right direction
        return super(SIPPortEditor, self).stepBy(steps)

    def validate(self, input, pos):
        state, input, pos = super(SIPPortEditor, self).validate(input, pos)
        if state == QValidator.State.Acceptable:
            value = int(input)
            if 0 < value < 1024:
                state = QValidator.State.Intermediate
            elif value == self.sibling.value() != 0:
                state = QValidator.State.Intermediate
        return state, input, pos


class AccountDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        account_info = index.data(Qt.ItemDataRole.UserRole)
        if not account_info.account.enabled:
            option.state &= ~QStyle.StateFlag.State_Enabled
        super(AccountDelegate, self).paint(painter, option, index)


class AccountListView(QListView):
    def __init__(self, parent=None):
        super(AccountListView, self).__init__(parent)
        self.setItemDelegate(AccountDelegate(self))
        # self.setDropIndicatorShown(False)

    def selectionChanged(self, selected, deselected):
        super(AccountListView, self).selectionChanged(selected, deselected)
        selection_model = self.selectionModel()
        selection = selection_model.selection()
        if selection_model.currentIndex() not in selection:
            index = selection.indexes()[0] if not selection.isEmpty() else self.model().index(-1)
            selection_model.setCurrentIndex(index, selection_model.SelectionFlag.Select)


class blocked_qt_signals(object):
    def __init__(self, qobject):
        self.qobject = qobject

    def __enter__(self):
        self.qobject.blockSignals(True)
        return self.qobject

    def __exit__(self, type, value, traceback):
        self.qobject.blockSignals(False)


class UnspecifiedOutboundProxy(object):
    host = ''
    port = 5060
    transport = 'UDP'


class UnspecifiedMSRPRelay(object):
    host = ''
    port = 0
    transport = 'TLS'


ui_class, base_class = uic.loadUiType(Resources.get('preferences.ui'))


@implementer(IObserver)
class PreferencesWindow(base_class, ui_class, metaclass=QSingleton):

    def __init__(self, account_model, parent=None):
        super(PreferencesWindow, self).__init__(parent)

        with Resources.directory:
            self.setupUi()

        self.setWindowTitle(translate('preferences_window', 'Blink Preferences'))

        self.account_list.setModel(account_model)
        self.delete_account_button.setEnabled(False)

        self.camera_preview.installEventFilter(self)

        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='SIPApplicationDidStart')

        # Dialogs
        self.add_account_dialog = AddAccountDialog(self)

        # Signals
        self.toolbar.actionTriggered.connect(self._SH_ToolbarActionTriggered)

        # Account
        self.account_list.selectionModel().selectionChanged.connect(self._SH_AccountListSelectionChanged)
        self.account_list.model().dataChanged.connect(self._SH_AccountListDataChanged)
        self.add_account_button.clicked.connect(self.show_add_account_dialog)
        self.delete_account_button.clicked.connect(self._SH_DeleteAccountButtonClicked)

        # Account information
        self.account_enabled_button.clicked.connect(self._SH_AccountEnabledButtonClicked)
        self.account_enabled_presence_button.clicked.connect(self._SH_AccountEnabledPresenceButtonClicked)
        self.account_enabled_mwi_button.clicked.connect(self._SH_AccountEnabledMWIButtonClicked)
        self.display_name_editor.editingFinished.connect(self._SH_DisplayNameEditorEditingFinished)
        self.password_editor.editingFinished.connect(self._SH_PasswordEditorEditingFinished)

        # Account media settings
        self.account_audio_codecs_list.itemChanged.connect(self._SH_AccountAudioCodecsListItemChanged)
        self.account_audio_codecs_list.model().rowsMoved.connect(self._SH_AccountAudioCodecsListModelRowsMoved)
        self.reset_account_audio_codecs_button.clicked.connect(self._SH_ResetAudioCodecsButtonClicked)
        self.account_video_codecs_list.itemChanged.connect(self._SH_AccountVideoCodecsListItemChanged)
        self.account_video_codecs_list.model().rowsMoved.connect(self._SH_AccountVideoCodecsListModelRowsMoved)
        self.reset_account_video_codecs_button.clicked.connect(self._SH_ResetVideoCodecsButtonClicked)
        self.inband_dtmf_button.clicked.connect(self._SH_InbandDTMFButtonClicked)
        self.rtp_encryption_button.clicked.connect(self._SH_RTPEncryptionButtonClicked)
        self.key_negotiation_button.activated[int].connect(self._SH_KeyNegotiationButtonActivated)

        # Account server settings
        self.always_use_my_proxy_button.clicked.connect(self._SH_AlwaysUseMyProxyButtonClicked)
        self.outbound_proxy_host_editor.editingFinished.connect(self._SH_OutboundProxyHostEditorEditingFinished)
        self.outbound_proxy_port.valueChanged[int].connect(self._SH_OutboundProxyPortValueChanged)
        self.outbound_proxy_transport_button.activated[int].connect(self._SH_OutboundProxyTransportButtonActivated)
        self.auth_username_editor.editingFinished.connect(self._SH_AuthUsernameEditorEditingFinished)
        self.always_use_my_msrp_relay_button.clicked.connect(self._SH_AlwaysUseMyMSRPRelayButtonClicked)
        self.msrp_relay_host_editor.editingFinished.connect(self._SH_MSRPRelayHostEditorEditingFinished)
        self.stun_server_list_editor.editingFinished.connect(self._SH_StunServerListEditorEditingFinished)
        self.msrp_relay_port.valueChanged[int].connect(self._SH_MSRPRelayPortValueChanged)
        self.msrp_relay_transport_button.activated[int].connect(self._SH_MSRPRelayTransportButtonActivated)
        self.voicemail_uri_editor.editingFinished.connect(self._SH_VoicemailURIEditorEditingFinished)
        self.xcap_root_editor.editingFinished.connect(self._SH_XCAPRootEditorEditingFinished)
        self.server_tools_url_editor.editingFinished.connect(self._SH_ServerToolsURLEditorEditingFinished)
        self.conference_server_editor.editingFinished.connect(self._SH_ConferenceServerEditorEditingFinished)
        self.enable_xcap_button.clicked.connect(self._SH_EnableXcapButtonClicked)

        # Account NAT traversal settings
        self.use_ice_button.clicked.connect(self._SH_UseICEButtonClicked)
        self.msrp_transport_button.activated[int].connect(self._SH_MSRPTransportButtonActivated)

        # Account advanced settings
        self.register_interval.valueChanged[int].connect(self._SH_RegisterIntervalValueChanged)
        self.publish_interval.valueChanged[int].connect(self._SH_PublishIntervalValueChanged)
        self.subscribe_interval.valueChanged[int].connect(self._SH_SubscribeIntervalValueChanged)
        self.reregister_button.clicked.connect(self._SH_ReregisterButtonClicked)
        self.idd_prefix_button.activated[int].connect(self._SH_IDDPrefixButtonActivated)
        self.prefix_button.activated[int].connect(self._SH_PrefixButtonActivated)
        self.account_tls_name_editor.editingFinished.connect(self._SH_TLSPeerNameEditorEditingFinished)

        # Account sms settings
        self.message_cpim_enabled_button.clicked.connect(self._SH_EnableMessageCPIMButtonClicked)
        self.message_iscomposing_enabled_button.clicked.connect(self._SH_EnableMessageIsComposingButtonClicked)
        self.message_imdn_enabled_button.clicked.connect(self._SH_EnableMessageIMDNButtonClicked)
        self.message_pgp_enabled_button.clicked.connect(self._SH_EnablePGPButtonClicked)
        self.message_replication_button.clicked.connect(self._SH_MessageReplicationButtonClicked)
        self.message_synchronization_button.clicked.connect(self._SH_MessageSynchronizationButtonClicked)
        self.history_url_editor.editingFinished.connect(self._SH_HistoryUrlEditorEditingFinshed)
        self.last_id_editor.editingFinished.connect(self._SH_LastIdEditorEditingFinished)

        # Audio devices
        self.audio_alert_device_button.activated[int].connect(self._SH_AudioAlertDeviceButtonActivated)
        self.audio_input_device_button.activated[int].connect(self._SH_AudioInputDeviceButtonActivated)
        self.audio_output_device_button.activated[int].connect(self._SH_AudioOutputDeviceButtonActivated)
        self.audio_sample_rate_button.activated[int].connect(self._SH_AudioSampleRateButtonActivated)
        self.enable_echo_cancelling_button.clicked.connect(self._SH_EnableEchoCancellingButtonClicked)
        self.tail_length_slider.valueChanged.connect(self._SH_TailLengthSliderValueChanged)

        # Audio codecs
        self.audio_codecs_list.itemChanged.connect(self._SH_AudioCodecsListItemChanged)
        self.audio_codecs_list.model().rowsMoved.connect(self._SH_AudioCodecsListModelRowsMoved)

        # Answering machine
        self.enable_answering_machine_button.clicked.connect(self._SH_EnableAnsweringMachineButtonClicked)
        self.answer_delay.valueChanged[int].connect(self._SH_AnswerDelayValueChanged)
        self.max_recording.valueChanged[int].connect(self._SH_MaxRecordingValueChanged)

        # Video devices
        self.video_camera_button.activated[int].connect(self._SH_VideoCameraButtonActivated)
        self.video_resolution_button.activated[int].connect(self._SH_VideoResolutionButtonActivated)
        self.video_framerate_button.activated[int].connect(self._SH_VideoFramerateButtonActivated)

        # Video codecs
        self.video_codecs_list.itemChanged.connect(self._SH_VideoCodecsListItemChanged)
        self.video_codecs_list.model().rowsMoved.connect(self._SH_VideoCodecsListModelRowsMoved)
        self.video_codec_bitrate_button.activated[int].connect(self._SH_VideoCodecBitrateButtonActivated)
        self.h264_profile_button.activated[int].connect(self._SH_H264ProfileButtonActivated)

        # Chat
        self.style_view.sizeChanged.connect(self._SH_StyleViewSizeChanged)
        self.style_view.page().mainFrame().contentsSizeChanged.connect(self._SH_StyleViewFrameContentsSizeChanged)

        self.style_button.activated[int].connect(self._SH_StyleButtonActivated)
        self.style_variant_button.activated[int].connect(self._SH_StyleVariantButtonActivated)
        self.style_show_icons_button.clicked.connect(self._SH_StyleShowIconsButtonClicked)

        self.style_font_button.currentIndexChanged[int].connect(self._SH_StyleFontButtonCurrentIndexChanged)
        self.style_font_size.valueChanged[int].connect(self._SH_StyleFontSizeValueChanged)
        self.style_default_font_button.clicked.connect(self._SH_StyleDefaultFontButtonClicked)

        self.auto_accept_chat_button.clicked.connect(self._SH_AutoAcceptChatButtonClicked)
        self.chat_message_alert_button.clicked.connect(self._SH_ChatMessageAlertButtonClicked)
        self.sms_replication_button.clicked.connect(self._SH_SMSReplicationButtonClicked)

        self.session_info_style_button.clicked.connect(self._SH_SessionInfoStyleButtonClicked)
        self.traffic_units_button.clicked.connect(self._SH_TrafficUnitsButtonClicked)

        # Screen sharing
        self.screen_sharing_scale_button.clicked.connect(self._SH_ScreenSharingScaleButtonClicked)
        self.screen_sharing_fullscreen_button.clicked.connect(self._SH_ScreenSharingFullscreenButtonClicked)
        self.screen_sharing_viewonly_button.clicked.connect(self._SH_ScreenSharingViewonlyButtonClicked)

        # File logging
        self.trace_sip_button.clicked.connect(self._SH_TraceSIPButtonClicked)
        self.trace_messaging_button.clicked.connect(self._SH_TraceMessagingButtonClicked)
        self.trace_msrp_button.clicked.connect(self._SH_TraceMSRPButtonClicked)
        self.trace_xcap_button.clicked.connect(self._SH_TraceXCAPButtonClicked)
        self.trace_notifications_button.clicked.connect(self._SH_TraceNotificationsButtonClicked)
        self.trace_pjsip_button.clicked.connect(self._SH_TracePJSIPButtonClicked)
        self.pjsip_trace_level.valueChanged[int].connect(self._SH_PJSIPTraceLevelValueChanged)
        self.clear_log_files_button.clicked.connect(self._SH_ClearLogFilesButtonClicked)

        # SIP and RTP
        self.sip_transports_button_group.buttonClicked.connect(self._SH_SIPTransportsButtonClicked)
        self.udp_port.valueChanged[int].connect(self._SH_UDPPortValueChanged)
        self.tcp_port.valueChanged[int].connect(self._SH_TCPPortValueChanged)
        self.tls_port.valueChanged[int].connect(self._SH_TLSPortValueChanged)
        self.media_ports_start.valueChanged[int].connect(self._SH_MediaPortsStartValueChanged)
        self.media_ports.valueChanged[int].connect(self._SH_MediaPortsValueChanged)

        # Files and directories
        self.screenshots_directory_browse_button.clicked.connect(self._SH_ScreenshotsDirectoryBrowseButtonClicked)
        self.transfers_directory_browse_button.clicked.connect(self._SH_TransfersDirectoryBrowseButtonClicked)

        # TLS settings
        self.tls_ca_file_editor.locationCleared.connect(self._SH_TLSCAFileEditorLocationCleared)
        self.tls_ca_file_browse_button.clicked.connect(self._SH_TLSCAFileBrowseButtonClicked)
        self.tls_cert_file_editor.locationCleared.connect(self._SH_TLSCertFileEditorLocationCleared)
        self.tls_cert_file_browse_button.clicked.connect(self._SH_TLSCertFileBrowseButtonClicked)
        self.tls_verify_server_button.clicked.connect(self._SH_TLSVerifyServerButtonClicked)

        # Auto answer
        self.auto_answer_interval.valueChanged[int].connect(self._SH_AutoAnswerIntervalChanged)
        self.account_auto_answer.clicked.connect(self._SH_AccountAutoAnswerChanged)

        # Interface
        self.history_name_and_uri_button.clicked.connect(self._SH_HistoryNameAndUriButtonClicked)
        self.language_button.activated[int].connect(self._SH_LanguageButtonActivated)
        self.show_messages_group_button.clicked.connect(self._SH_ShowMessagesGroupButtonClicked)

        # Setup initial state (show the accounts page right after start)
        self.accounts_action.trigger()
        self.account_tab_widget.setCurrentIndex(0)

    def setupUi(self):
        super(PreferencesWindow, self).setupUi(self)

        # Accounts
        self.key_negotiation_button.clear()
        self.key_negotiation_button.addItem(translate('preferences_window', 'Opportunistic'), 'opportunistic')
        self.key_negotiation_button.addItem(translate('preferences_window', 'ZRTP'), 'zrtp')
        self.key_negotiation_button.addItem(translate('preferences_window', 'SDES optional'), 'sdes_optional')
        self.key_negotiation_button.addItem(translate('preferences_window', 'SDES mandatory'), 'sdes_mandatory')

        # Audio

        # Hide the tail_length slider as it is only useful for debugging -Dan
        self.tail_length_label.hide()
        self.tail_length_slider.hide()
        self.tail_length_value_label.hide()

        # Hide the controls for the features that are not yet implemented -Dan
        self.answering_machine_group_box.hide()
        self.sms_replication_button.hide()

        # Video
        size_policy = self.camera_preview.sizePolicy()
        size_policy.setHeightForWidth(True)
        self.camera_preview.setSizePolicy(size_policy)
        self.camera_preview.mirror = True

        self.video_resolution_button.clear()
        self.video_resolution_button.addItem('HD 720p', '1280x720')
        self.video_resolution_button.addItem('VGA', '640x480')
        self.h264_level_map = {'1280x720': '3.1', '640x480': '3.0'}

        self.video_framerate_button.clear()
        for rate in range(10, 31, 5):
            self.video_framerate_button.addItem('%d fps' % rate, rate)

        self.video_codec_bitrate_button.clear()
        self.video_codec_bitrate_button.addItem(translate('preferences_window', 'automatic'), None)
        for bitrate in (1.0, 2.0, 4.0):
            self.video_codec_bitrate_button.addItem('%g Mbps' % bitrate, bitrate)

        self.h264_profile_button.clear()
        for profile in H264Profile.valid_values:
            self.h264_profile_button.addItem(profile, profile)

        # Chat
        self.style_view.template = open(Resources.get('chat/template.html')).read()

        self.style_button.clear()
        self.style_variant_button.clear()

        styles_path = Resources.get('chat/styles')
        for style_name in os.listdir(styles_path):
            try:
                style = ChatMessageStyle(style_name)
            except ChatStyleError:
                pass
            else:
                self.style_button.addItem(style_name, style)

        self.section_group = QActionGroup(self)
        self.section_group.setExclusive(True)
        for index, action in enumerate(action for action in self.toolbar.actions() if not action.isSeparator()):
            action.index = index
            self.section_group.addAction(action)

        for index in range(self.idd_prefix_button.count()):
            text = self.idd_prefix_button.itemText(index)
            self.idd_prefix_button.setItemData(index, None if text == "+" else text)
        for index in range(self.prefix_button.count()):
            text = self.prefix_button.itemText(index)
            self.prefix_button.setItemData(index, None if text == "None" else text)

        self.voicemail_uri_editor.setValidator(SIPAddressValidator(self))
        self.xcap_root_editor.setValidator(XCAPRootValidator(self))
        self.server_tools_url_editor.setValidator(WebURLValidator(self))
        self.conference_server_editor.setValidator(HostnameValidator(self))
        self.idd_prefix_button.setValidator(IDDPrefixValidator(self))
        self.prefix_button.setValidator(PrefixValidator(self))

        # Languages
        self.language_button.clear()
        languages_path = Resources.get('i18n')
        self.language_button.addItem(translate('preferences_window', 'Automatic (Default)'), Language('default'))
        self.language_button.addItem('English', Language('en'))
        for language_file in os.listdir(languages_path):
            try:
                language = Language(language_file)
            except LanguageError:
                pass
            else:
                self.language_button.addItem(language.name, language)

        # Adding the button group in designer has issues on Ubuntu 10.04
        self.sip_transports_button_group = QButtonGroup(self)
        self.sip_transports_button_group.setObjectName("sip_transports_button_group")
        self.sip_transports_button_group.setExclusive(False)
        self.sip_transports_button_group.addButton(self.enable_udp_button)
        self.sip_transports_button_group.addButton(self.enable_tcp_button)
        self.sip_transports_button_group.addButton(self.enable_tls_button)

        self.enable_udp_button.name = 'udp'
        self.enable_tcp_button.name = 'tcp'
        self.enable_tls_button.name = 'tls'

        self.tcp_port.sibling = self.tls_port
        self.tls_port.sibling = self.tcp_port

        # Adjust some minimum label widths in order to better align settings in different group boxes, widgets or tabs

        # account server and network tab
        font_metrics = self.outbound_proxy_label.fontMetrics()  # we assume all labels have the same font
        labels = (self.outbound_proxy_label, self.auth_username_label, self.msrp_relay_label,
                  self.voicemail_uri_label, self.xcap_root_label, self.server_tools_url_label,
                  self.conference_server_label, self.msrp_transport_label)
        text_width = max(font_metrics.size(Qt.TextFlag.TextSingleLine, label.text()).width() for label in labels) + 15
        self.outbound_proxy_label.setMinimumWidth(text_width)
        self.msrp_transport_label.setMinimumWidth(text_width)

        # account advanced tab
        font_metrics = self.register_interval_label.fontMetrics()  # we assume all labels have the same font
        labels = (self.register_interval_label, self.publish_interval_label, self.subscribe_interval_label,
                  self.idd_prefix_label, self.prefix_label)
        text_width = max(font_metrics.size(Qt.TextFlag.TextSingleLine, label.text()).width() for label in labels) + 15
        self.register_interval_label.setMinimumWidth(text_width)
        self.idd_prefix_label.setMinimumWidth(text_width)
        self.tls_cert_file_label.setMinimumWidth(text_width)

        # audio settings
        font_metrics = self.answer_delay_label.fontMetrics()  # we assume all labels have the same font
        labels = (self.audio_input_device_label, self.audio_output_device_label, self.audio_alert_device_label, self.audio_sample_rate_label,
                  self.answer_delay_label, self.max_recording_label, self.unavailable_message_label)
        text_width = max(font_metrics.size(Qt.TextFlag.TextSingleLine, label.text()).width() for label in labels)
        self.audio_input_device_label.setMinimumWidth(text_width)
        self.answer_delay_label.setMinimumWidth(text_width)

        # Adjust the combo boxes for themes with too much padding (like the default theme on Ubuntu 10.04)
        combo_box = self.audio_input_device_button
        option = QStyleOptionComboBox()
        combo_box.initStyleOption(option)
        wide_padding = (combo_box.height() - combo_box.style().subControlRect(QStyle.ComplexControl.CC_ComboBox, option, QStyle.SubControl.SC_ComboBoxEditField, combo_box).height() >= 10)
        if False and wide_padding: # TODO: review later and decide if its worth or not -Dan
            self.audio_alert_device_button.setStyleSheet("""QComboBox { padding: 4px 4px 4px 4px; }""")
            self.audio_input_device_button.setStyleSheet("""QComboBox { padding: 4px 4px 4px 4px; }""")
            self.audio_output_device_button.setStyleSheet("""QComboBox { padding: 4px 4px 4px 4px; }""")
            self.audio_sample_rate_button.setStyleSheet("""QComboBox { padding: 4px 4px 4px 4px; }""")
            self.unavailable_message_button.setStyleSheet("""QComboBox { padding: 4px 4px 4px 4px; }""")

        self.history_url_editor.setValidator(WebURLValidator(self))

    def eventFilter(self, watched, event):
        if watched is self.camera_preview:
            event_type = event.type()
            if event_type == QEvent.Type.Show:
                self.camera_preview.producer = SIPApplication.video_device.producer
            elif event_type == QEvent.Type.Hide:
                self.camera_preview.producer = None
        return False

    def closeEvent(self, event):
        super(PreferencesWindow, self).closeEvent(event)
        self.add_account_dialog.close()

    @property
    def account_msrp_relay(self):
        host = self.msrp_relay_host_editor.text()
        port = self.msrp_relay_port.value()
        transport = self.msrp_relay_transport_button.currentText().lower()
        return MSRPRelayAddress(host, port, transport) if host else None

    @property
    def account_outbound_proxy(self):
        host = self.outbound_proxy_host_editor.text()
        port = self.outbound_proxy_port.value()
        transport = self.outbound_proxy_transport_button.currentText().lower()
        return SIPProxyAddress(host, port, transport) if host else None

    @property
    def selected_account(self):
        try:
            selected_index = self.account_list.selectionModel().selectedIndexes()[0]
        except IndexError:
            return None
        else:
            return selected_index.data(Qt.ItemDataRole.UserRole).account

    def _sync_defaults(self):
        settings = SIPSimpleSettings()
        account_manager = AccountManager()

        if settings.rtp.audio_codec_order is not SIPSimpleSettings.rtp.audio_codec_order.default or settings.rtp.audio_codec_list is not SIPSimpleSettings.rtp.audio_codec_list.default:
            # user has a non-default codec order, we need to sync with the new settings
            added_codecs = set(SIPSimpleSettings.rtp.audio_codec_order.default).difference(settings.rtp.audio_codec_order)
            removed_codecs = set(settings.rtp.audio_codec_order).difference(SIPSimpleSettings.rtp.audio_codec_order.default)
            removed_codecs.update(set(settings.rtp.audio_codec_list).difference(SIPSimpleSettings.rtp.audio_codec_list.default))

            if added_codecs:
                settings.rtp.audio_codec_order = DefaultValue  # reset codec order
                settings.rtp.audio_codec_list  = DefaultValue  # reset codec list
                settings.save()
            elif removed_codecs:
                codec_order = [codec for codec in settings.rtp.audio_codec_order if codec not in removed_codecs]
                codec_list  = [codec for codec in settings.rtp.audio_codec_list if codec not in removed_codecs]
                if codec_order == SIPSimpleSettings.rtp.audio_codec_order.default:
                    codec_order = DefaultValue
                if codec_list == SIPSimpleSettings.rtp.audio_codec_list.default:
                    codec_list = DefaultValue
                settings.rtp.audio_codec_order = codec_order
                settings.rtp.audio_codec_list  = codec_list
                settings.save()

        for account in (account for account in account_manager.iter_accounts() if account.rtp.audio_codec_order is not None):
            # user has a non-default codec order, we need to sync with the new settings
            added_codecs = set(SIPSimpleSettings.rtp.audio_codec_order.default).difference(account.rtp.audio_codec_order)
            removed_codecs = set(account.rtp.audio_codec_order).difference(SIPSimpleSettings.rtp.audio_codec_order.default)
            if added_codecs:
                account.rtp.audio_codec_order = DefaultValue  # reset codec order
                account.rtp.audio_codec_list  = DefaultValue  # reset codec list
                account.save()
            elif removed_codecs:
                codec_order = [codec for codec in account.rtp.audio_codec_order if codec not in removed_codecs]
                codec_list  = [codec for codec in account.rtp.audio_codec_list if codec not in removed_codecs]
                if codec_order == SIPSimpleSettings.rtp.audio_codec_order.default and codec_list == SIPSimpleSettings.rtp.audio_codec_list.default:
                    codec_order = DefaultValue
                    codec_list  = DefaultValue
                account.rtp.audio_codec_order = codec_order
                account.rtp.audio_codec_list  = codec_list
                account.save()

        if settings.rtp.video_codec_order is not SIPSimpleSettings.rtp.video_codec_order.default or settings.rtp.video_codec_list is not SIPSimpleSettings.rtp.video_codec_list.default:
            # user has a non-default codec order, we need to sync with the new settings
            added_codecs = set(SIPSimpleSettings.rtp.video_codec_order.default).difference(settings.rtp.video_codec_order)
            removed_codecs = set(settings.rtp.video_codec_order).difference(SIPSimpleSettings.rtp.video_codec_order.default)
            if added_codecs:
                settings.rtp.video_codec_order = DefaultValue  # reset codec order
                settings.rtp.video_codec_list  = DefaultValue  # reset codec list
                settings.save()
            elif removed_codecs:
                codec_order = [codec for codec in settings.rtp.video_codec_order if codec not in removed_codecs]
                codec_list  = [codec for codec in settings.rtp.video_codec_list if codec not in removed_codecs]
                if codec_order == SIPSimpleSettings.rtp.video_codec_order.default:
                    codec_order = DefaultValue
                if codec_list == SIPSimpleSettings.rtp.video_codec_list.default:
                    codec_list = DefaultValue
                settings.rtp.video_codec_order = codec_order
                settings.rtp.video_codec_list  = codec_list
                settings.save()

        for account in (account for account in account_manager.iter_accounts() if account.rtp.video_codec_order is not None):
            # user has a non-default codec order, we need to sync with the new settings
            added_codecs = set(SIPSimpleSettings.rtp.video_codec_order.default).difference(account.rtp.video_codec_order)
            removed_codecs = set(account.rtp.video_codec_order).difference(SIPSimpleSettings.rtp.video_codec_order.default)
            if added_codecs:
                account.rtp.video_codec_order = DefaultValue  # reset codec order
                account.rtp.video_codec_list  = DefaultValue  # reset codec list
                account.save()
            elif removed_codecs:
                codec_order = [codec for codec in account.rtp.video_codec_order if codec not in removed_codecs]
                codec_list  = [codec for codec in account.rtp.video_codec_list if codec not in removed_codecs]
                if codec_order == SIPSimpleSettings.rtp.video_codec_order.default and codec_list == SIPSimpleSettings.rtp.video_codec_list.default:
                    codec_order = DefaultValue
                    codec_list  = DefaultValue
                account.rtp.video_codec_order = codec_order
                account.rtp.video_codec_list  = codec_list
                account.save()

    def load_audio_devices(self):
        settings = SIPSimpleSettings()

        class Separator: pass

        self.audio_input_device_button.clear()
        self.audio_input_device_button.addItem(translate('preferences_window', 'System Default'), 'system_default')
        self.audio_input_device_button.insertSeparator(1)
        self.audio_input_device_button.setItemData(1, Separator)  # prevent the separator from being selected (must have different itemData than the None device)
        for device in SIPApplication.engine.input_devices:
            self.audio_input_device_button.addItem(device, device)
        self.audio_input_device_button.addItem(translate('preferences_window', 'None'), None)
        self.audio_input_device_button.setCurrentIndex(self.audio_input_device_button.findData(settings.audio.input_device))

        self.audio_output_device_button.clear()
        self.audio_output_device_button.addItem(translate('preferences_window', 'System Default'), 'system_default')
        self.audio_output_device_button.insertSeparator(1)
        self.audio_output_device_button.setItemData(1, Separator)  # prevent the separator from being selected (must have different itemData than the None device)
        for device in SIPApplication.engine.output_devices:
            self.audio_output_device_button.addItem(device, device)
        self.audio_output_device_button.addItem(translate('preferences_window', 'None'), None)
        self.audio_output_device_button.setCurrentIndex(self.audio_output_device_button.findData(settings.audio.output_device))

        self.audio_alert_device_button.clear()
        self.audio_alert_device_button.addItem(translate('preferences_window', 'System Default'), 'system_default')
        self.audio_alert_device_button.insertSeparator(1)
        self.audio_alert_device_button.setItemData(1, Separator)  # prevent the separator from being selected (must have different itemData than the None device)
        for device in SIPApplication.engine.output_devices:
            self.audio_alert_device_button.addItem(device, device)
        self.audio_alert_device_button.addItem(translate('preferences_window', 'None'), None)
        self.audio_alert_device_button.setCurrentIndex(self.audio_alert_device_button.findData(settings.audio.alert_device))

    def load_video_devices(self):
        settings = SIPSimpleSettings()

        class Separator: pass

        self.video_camera_button.clear()
        self.video_camera_button.addItem(translate('preferences_window', 'System Default'), 'system_default')
        self.video_camera_button.insertSeparator(1)
        self.video_camera_button.setItemData(1, Separator)  # prevent the separator from being selected (must have different itemData than the None device)
        for device in SIPApplication.engine.video_devices:
            self.video_camera_button.addItem(device, device)
        self.video_camera_button.addItem(translate('preferences_window', 'None'), None)
        self.video_camera_button.setCurrentIndex(self.video_camera_button.findData(settings.video.device))

    def load_settings(self):
        """Load settings from configuration into the UI controls"""
        settings = SIPSimpleSettings()
        blink_settings = BlinkSettings()

        # Audio devices
        self.load_audio_devices()
        self.enable_echo_cancelling_button.setChecked(settings.audio.echo_canceller.enabled)
        with blocked_qt_signals(self.tail_length_slider):
            self.tail_length_slider.setValue(settings.audio.echo_canceller.tail_length)
        self.audio_sample_rate_button.clear()
        for rate in SIPSimpleSettings.audio.sample_rate.type.valid_values:
            self.audio_sample_rate_button.addItem(str(rate), rate)
        self.audio_sample_rate_button.setCurrentIndex(self.audio_sample_rate_button.findText(str(settings.audio.sample_rate)))

        # Audio codecs
        with blocked_qt_signals(self.audio_codecs_list):
            self.audio_codecs_list.clear()
            for codec in settings.rtp.audio_codec_order:
                item = QListWidgetItem(codec, self.audio_codecs_list)
                item.setCheckState(Qt.CheckState.Checked if codec in settings.rtp.audio_codec_list else Qt.CheckState.Unchecked)

        # Answering Machine settings
        self.enable_answering_machine_button.setChecked(settings.answering_machine.enabled)
        with blocked_qt_signals(self.answer_delay):
            self.answer_delay.setValue(settings.answering_machine.answer_delay)
        with blocked_qt_signals(self.max_recording):
            self.max_recording.setValue(settings.answering_machine.max_recording)
        # TODO: load unavailable message -Dan

        # Video devices
        self.load_video_devices()

        self.video_resolution_button.setCurrentIndex(self.video_resolution_button.findData(str(settings.video.resolution)))
        self.video_framerate_button.setCurrentIndex(self.video_framerate_button.findData(settings.video.framerate))

        # Video codecs
        with blocked_qt_signals(self.video_codecs_list):
            self.video_codecs_list.clear()
            for codec in settings.rtp.video_codec_order:
                item = QListWidgetItem(codec, self.video_codecs_list)
                item.setCheckState(Qt.CheckState.Checked if codec in settings.rtp.video_codec_list else Qt.CheckState.Unchecked)

        self.h264_profile_button.setCurrentIndex(self.h264_profile_button.findData(str(settings.video.h264.profile)))
        self.video_codec_bitrate_button.setCurrentIndex(self.video_codec_bitrate_button.findData(settings.video.max_bitrate))

        # Chat
        style_index = self.style_button.findText(blink_settings.chat_window.style)
        if style_index == -1:
            style_index = 0
            blink_settings.chat_window.style = self.style_button.itemText(style_index)
            blink_settings.chat_window.style_variant = None
            blink_settings.save()
        style = self.style_button.itemData(style_index)
        self.style_button.setCurrentIndex(style_index)
        self.style_variant_button.clear()
        for variant in style.variants:
            self.style_variant_button.addItem(variant)
        variant_index = self.style_variant_button.findText(blink_settings.chat_window.style_variant or style.default_variant)
        if variant_index == -1:
            variant_index = self.style_variant_button.findText(style.default_variant)
            blink_settings.chat_window.style_variant = None
            blink_settings.save()
        self.style_variant_button.setCurrentIndex(variant_index)
        self.style_show_icons_button.setChecked(blink_settings.chat_window.show_user_icons)
        self.update_chat_preview()

        with blocked_qt_signals(self.style_font_button):
            self.style_font_button.setCurrentFont(QFont(blink_settings.chat_window.font or style.font_family))
        with blocked_qt_signals(self.style_font_size):
            self.style_font_size.setValue(blink_settings.chat_window.font_size or style.font_size)
        self.style_default_font_button.setEnabled(blink_settings.chat_window.font is not None or blink_settings.chat_window.font_size is not None)

        self.auto_accept_chat_button.setChecked(settings.chat.auto_accept)
        self.chat_message_alert_button.setChecked(settings.sounds.play_message_alerts)
        self.sms_replication_button.setChecked(settings.chat.sms_replication)

        self.session_info_style_button.setChecked(blink_settings.chat_window.session_info.alternate_style)
        self.traffic_units_button.setChecked(blink_settings.chat_window.session_info.bytes_per_second)

        # Screen sharing settings
        self.screen_sharing_scale_button.setChecked(blink_settings.screen_sharing.scale)
        self.screen_sharing_fullscreen_button.setChecked(blink_settings.screen_sharing.open_fullscreen)
        self.screen_sharing_viewonly_button.setChecked(blink_settings.screen_sharing.open_viewonly)

        # File logging settings
        self.trace_sip_button.setChecked(settings.logs.trace_sip)
        self.trace_messaging_button.setChecked(settings.logs.trace_messaging)
        self.trace_msrp_button.setChecked(settings.logs.trace_msrp)
        self.trace_xcap_button.setChecked(settings.logs.trace_xcap)
        self.trace_notifications_button.setChecked(settings.logs.trace_notifications)
        self.trace_pjsip_button.setChecked(settings.logs.trace_pjsip)
        with blocked_qt_signals(self.pjsip_trace_level):
            self.pjsip_trace_level.setValue(limit(settings.logs.pjsip_level, min=0, max=5))

        # Advanced settings
        for button in self.sip_transports_button_group.buttons():
            button.setChecked(button.name in settings.sip.transport_list)

        if settings.sip.tcp_port and settings.sip.tcp_port == settings.sip.tls_port:
            log.warning("the SIP TLS and TCP ports cannot be the same")
            settings.sip.tls_port = settings.sip.tcp_port + 1 if settings.sip.tcp_port < 65535 else 65534
            settings.save()

        with blocked_qt_signals(self.udp_port):
            self.udp_port.setValue(settings.sip.udp_port)
        with blocked_qt_signals(self.tcp_port):
            self.tcp_port.setValue(settings.sip.tcp_port)
        with blocked_qt_signals(self.tls_port):
            self.tls_port.setValue(settings.sip.tls_port)
        with blocked_qt_signals(self.media_ports_start):
            self.media_ports_start.setValue(settings.rtp.port_range.start)
        with blocked_qt_signals(self.media_ports):
            self.media_ports.setValue(settings.rtp.port_range.end - settings.rtp.port_range.start)
        with blocked_qt_signals(self.auto_answer_interval):
            self.auto_answer_interval.setValue(settings.sip.auto_answer_interval)

        self.screenshots_directory_editor.setText(blink_settings.screenshots_directory or '')
        self.transfers_directory_editor.setText(blink_settings.transfers_directory or '')
        self.tls_ca_file_editor.setText(settings.tls.ca_list or '')
        self.tls_cert_file_editor.setText(settings.tls.certificate or '')
        self.tls_verify_server_button.setChecked(settings.tls.verify_server)

        self.history_name_and_uri_button.setChecked(blink_settings.interface.show_history_name_and_uri)
        self.show_messages_group_button.setChecked(blink_settings.interface.show_messages_group)

        language_index = self.language_button.findText(Language.mapping[blink_settings.interface.language])
        if language_index == -1:
            language_index = 0
            blink_settings.interface.language = self.language_button.itemData(language_index).language_code
            blink_settings.save()
        self.language_button.setCurrentIndex(language_index)

    def load_account_settings(self, account):
        """Load the account settings from configuration into the UI controls"""
        settings = SIPSimpleSettings()
        bonjour_account = BonjourAccount()

        # Account information tab
        self.account_enabled_button.setChecked(account.enabled)
        self.account_enabled_button.setEnabled(True if account is not bonjour_account else BonjourAccount.mdns_available)

        self.account_enabled_presence_button.setEnabled(account is not bonjour_account)
        self.account_enabled_presence_button.setChecked(account.presence.enabled if account is not bonjour_account else False)

        self.account_enabled_mwi_button.setEnabled(account is not bonjour_account)
        self.account_enabled_mwi_button.setChecked(account.message_summary.enabled if account is not bonjour_account else False)

        self.display_name_editor.setText(account.display_name or '')

        if account is not bonjour_account:
            self.password_editor.setText(account.auth.password)
            selected_index = self.account_list.selectionModel().selectedIndexes()[0]
            selected_account_info = self.account_list.model().data(selected_index, Qt.ItemDataRole.UserRole)
            if not account.enabled:
                selected_account_info.registration_state = None
                selected_account_info.registrar = None

            if selected_account_info.registration_state:
                if selected_account_info.registration_state == 'succeeded' and selected_account_info.registrar is not None:
                    self.account_registration_label.setText(translate('preferences_window', 'Registered at %s') % selected_account_info.registrar)
                else:
                    self.account_registration_label.setText(translate('preferences_window', 'Registration %s') % selected_account_info.registration_state.title())
            else:
                self.account_registration_label.setText(translate('preferences_window', 'Not Registered'))
        else:
            self.account_registration_label.setText('')

        # Media tab
        with blocked_qt_signals(self.account_audio_codecs_list):
            self.account_audio_codecs_list.clear()
            audio_codec_order = account.rtp.audio_codec_order or settings.rtp.audio_codec_order
            audio_codec_list = account.rtp.audio_codec_list or settings.rtp.audio_codec_list
            for codec in audio_codec_order:
                item = QListWidgetItem(codec, self.account_audio_codecs_list)
                item.setCheckState(Qt.CheckState.Checked if codec in audio_codec_list else Qt.CheckState.Unchecked)

        with blocked_qt_signals(self.account_video_codecs_list):
            self.account_video_codecs_list.clear()
            video_codec_order = account.rtp.video_codec_order or settings.rtp.video_codec_order
            video_codec_list = account.rtp.video_codec_list or settings.rtp.video_codec_list
            for codec in video_codec_order:
                item = QListWidgetItem(codec, self.account_video_codecs_list)
                item.setCheckState(Qt.CheckState.Checked if codec in video_codec_list else Qt.CheckState.Unchecked)

        self.reset_account_audio_codecs_button.setEnabled(account.rtp.audio_codec_order is not None)
        self.reset_account_video_codecs_button.setEnabled(account.rtp.video_codec_order is not None)

        self.inband_dtmf_button.setChecked(account.rtp.inband_dtmf)
        self.rtp_encryption_button.setChecked(account.rtp.encryption.enabled)
        self.key_negotiation_button.setEnabled(account.rtp.encryption.enabled)
        self.key_negotiation_button.setCurrentIndex(self.key_negotiation_button.findData(account.rtp.encryption.key_negotiation))

        self.account_auto_answer.setChecked(account.sip.auto_answer)

        # SMS settings tab, also relevant for bonjour
        self.message_cpim_enabled_button.setChecked(account.sms.use_cpim)
        self.message_iscomposing_enabled_button.setChecked(account.sms.enable_iscomposing)
        self.message_imdn_enabled_button.setEnabled(account.sms.use_cpim)
        self.message_imdn_enabled_button.setChecked(account.sms.enable_imdn)
        self.message_pgp_enabled_button.setChecked(account.sms.enable_pgp)

        if account is not bonjour_account:
            self.account_auto_answer.setText(translate('preferences_window', 'Auto answer from allowed contacts'))
            # Server settings tab
            self.always_use_my_proxy_button.setChecked(account.sip.always_use_my_proxy)
            outbound_proxy = account.sip.outbound_proxy or UnspecifiedOutboundProxy
            self.outbound_proxy_host_editor.setText(outbound_proxy.host)
            if account.nat_traversal.stun_server_list:
                stun_server_list = ", ".join('%s:%s' % (s.host, s.port) for s in account.nat_traversal.stun_server_list)
            else:
                stun_server_list = ""
            self.stun_server_list_editor.setText(stun_server_list)

            with blocked_qt_signals(self.outbound_proxy_port):
                self.outbound_proxy_port.setValue(outbound_proxy.port)
            self.outbound_proxy_transport_button.setCurrentIndex(self.outbound_proxy_transport_button.findText(outbound_proxy.transport.upper()))
            self.auth_username_editor.setText(account.auth.username or '')

            self.always_use_my_msrp_relay_button.setChecked(account.nat_traversal.use_msrp_relay_for_outbound)
            msrp_relay = account.nat_traversal.msrp_relay or UnspecifiedMSRPRelay
            self.msrp_relay_host_editor.setText(msrp_relay.host)
            with blocked_qt_signals(self.msrp_relay_port):
                self.msrp_relay_port.setValue(msrp_relay.port)
            self.msrp_relay_transport_button.setCurrentIndex(self.msrp_relay_transport_button.findText(msrp_relay.transport.upper()))

            self.enable_xcap_button.setChecked(account.xcap.enabled)
            self.voicemail_uri_editor.setText(account.message_summary.voicemail_uri or '')
            if not account.xcap.enabled:
                self.xcap_root_editor.setEnabled(False)
            self.xcap_root_editor.setText(account.xcap.xcap_root or '')
            self.server_tools_url_editor.setText(account.server.settings_url or '')
            self.conference_server_editor.setText(account.server.conference_server or '')

            # Network tab
            self.use_ice_button.setChecked(account.nat_traversal.use_ice)
            self.msrp_transport_button.setCurrentIndex(self.msrp_transport_button.findText(account.msrp.transport.upper()))

            # Advanced tab
            self.account_tls_name_editor.setText(account.sip.tls_name or account.id.domain)

            with blocked_qt_signals(self.register_interval):
                self.register_interval.setValue(account.sip.register_interval)
            with blocked_qt_signals(self.publish_interval):
                self.publish_interval.setValue(account.sip.publish_interval)
            with blocked_qt_signals(self.subscribe_interval):
                self.subscribe_interval.setValue(account.sip.subscribe_interval)
            self.reregister_button.setEnabled(account.enabled)

            item_text = account.pstn.idd_prefix or '+'
            index = self.idd_prefix_button.findText(item_text)
            if index == -1:
                self.idd_prefix_button.addItem(item_text)
            self.idd_prefix_button.setCurrentIndex(self.idd_prefix_button.findText(item_text))

            item_text = account.pstn.prefix or translate('preferences_window', 'None')
            index = self.prefix_button.findText(item_text)
            if index == -1:
                self.prefix_button.addItem(item_text)
            self.prefix_button.setCurrentIndex(self.prefix_button.findText(item_text))
            self._update_pstn_example_label()

            # Messages tab
            self.message_replication_button.show()
            self.message_synchronization_button.show()
            self.history_url_editor.show()
            self.history_url_label.show()
            self.last_id_editor.show()
            self.last_id_label.show()
            self.history_label.show()
            self.history_line.show()
            self.message_replication_button.setChecked(account.sms.enable_message_replication)
            self.message_synchronization_button.setChecked(account.sms.enable_history_synchronization)
            self.history_url_editor.setEnabled(account.sms.enable_history_synchronization)
            self.history_url_editor.setText(account.sms.history_synchronization_url)
            self.last_id_editor.setEnabled(account.sms.enable_history_synchronization)
            self.last_id_editor.setText(account.sms.history_synchronization_id)
        else:
            self.account_auto_answer.setText(translate('preferences_window', 'Auto answer from all neighbours'))

            self.message_replication_button.hide()
            self.message_synchronization_button.hide()
            self.history_url_editor.hide()
            self.history_url_label.hide()
            self.last_id_editor.hide()
            self.last_id_label.hide()
            self.history_label.hide()
            self.history_line.hide()

    def update_chat_preview(self):
        blink_settings = BlinkSettings()

        style = self.style_button.itemData(self.style_button.currentIndex())
        style_variant = self.style_variant_button.itemText(self.style_variant_button.currentIndex())
        font_family = blink_settings.chat_window.font or style.font_family
        font_size = blink_settings.chat_window.font_size or style.font_size
        user_icons = 'show-icons' if blink_settings.chat_window.show_user_icons else 'hide-icons'

        self.style_view.setHtml(self.style_view.template.format(base_url=FileURL(style.path) + '/', style_url=style_variant + '.css', font_family=font_family, font_size=font_size), baseUrl=QUrl.fromLocalFile(os.path.abspath(sys.argv[0])))
        self.chat_element = self.style_view.page().mainFrame().findFirstElement('#chat')
        self.chat_js = ChatJSInterface(self.style_view.page())
        self.style_view.last_message = None

        def add_message(message):
            if message.is_related_to(self.style_view.last_message):
                message.consecutive = True

                html_message = message.to_html(style, user_icons=user_icons)
                self.chat_js.replace_element('#insert', html_message)
            else:
                html_message = message.to_html(style, user_icons=user_icons)
                self.chat_js.append_message_to_chat(html_message)
            self.style_view.last_message = message

        ruby = ChatSender("Ruby", 'ruby@example.com', Resources.get('icons/avatar-ruby.png'))
        nate = ChatSender("Nate", 'nate@example.net', Resources.get('icons/avatar-nate.png'))

        messages = [ChatMessage("Andrew stepped into the room cautiously. The air was stale as if the place has not been visited in years and he had an acute feeling of being watched. "
                                "Was this the place he was looking for, the place holding the answers he looked for so long? He was hopeful but felt uneasy about it.", ruby, 'incoming'),
                    ChatMessage("Hey Ruby. Is this from the new book you're working on? Looks like it will be another interesting story to read :)", nate, 'outgoing'),
                    ChatMessage("Yeah. But I'm kind of lacking inspiration right now and the book needs to be finished in a month :(", ruby, 'incoming'),
                    ChatMessage("I think you put too much pressure on yourself. What about we get out for a bit? Watch a movie, chat about everyday events for a bit...", nate, 'outgoing'),
                    ChatMessage("It could help you take your mind off of things and relax. We can meet at the usual spot in an hour if you want.", nate, 'outgoing'),
                    ChatMessage("You may be right. Maybe that's what I need indeed. See you there.", ruby, 'incoming'),
                    ChatEvent("Ruby has left the conversation")]

        for message in messages:
            add_message(message)
        self._align_style_preview(True)
        del self.style_view.last_message

    def show(self):
        selection_model = self.account_list.selectionModel()
        if not selection_model.selectedIndexes():
            model = self.account_list.model()
            account_manager = AccountManager()
            default_account = account_manager.default_account
            try:
                index = next(index for index, account in enumerate(model.accounts) if account is default_account)
            except StopIteration:
                index = 0
            selection_model.select(model.index(index), selection_model.SelectionFlag.ClearAndSelect)
        self._update_logs_size_label()
        super(PreferencesWindow, self).show()
        self.raise_()
        self.activateWindow()

    def show_for_accounts(self):
        self.accounts_action.trigger()
        self.show()

    def show_add_account_dialog(self):
        self.add_account_dialog.open_for_add()

    def show_create_account_dialog(self):
        self.add_account_dialog.open_for_create()

    @staticmethod
    def _normalize_binary_size(size):
        """Return a human friendly string representation of size as a power of 2"""
        infinite = float('infinity')
        boundaries = [(             1024, '%d bytes',               1),
                      (          10*1024, '%.2f KB',           1024.0),  (     1024*1024, '%.1f KB',           1024.0),
                      (     10*1024*1024, '%.2f MB',      1024*1024.0),  (1024*1024*1024, '%.1f MB',      1024*1024.0),
                      (10*1024*1024*1024, '%.2f GB', 1024*1024*1024.0),  (      infinite, '%.1f GB', 1024*1024*1024.0)]
        for boundary, format, divisor in boundaries:
            if size < boundary:
                return format % (size/divisor,)
        else:
            return "%d bytes" % size

    def _update_logs_size_label(self):
        logs_size = 0
        for path, dirs, files in os.walk(os.path.join(ApplicationData.directory, 'logs')):
            for name in dirs:
                try:
                    logs_size += os.stat(os.path.join(path, name)).st_size
                except (OSError, IOError):
                    pass
            for name in files:
                try:
                    logs_size += os.stat(os.path.join(path, name)).st_size
                except (OSError, IOError):
                    pass
        self.log_files_size_label.setText(translate('preferences_window', "There are currently %s of log files") % self._normalize_binary_size(logs_size))

    def _update_pstn_example_label(self):
        prefix = self.prefix_button.currentText()
        idd_prefix = self.idd_prefix_button.currentText()
        self.pstn_example_transformed_label.setText("%s%s442079460000" % ('' if prefix == 'None' else prefix, idd_prefix))

    def _process_height(self, height, scroll=False):
        widget_height = self.style_view.size().height()
        content_height = height
        if widget_height > content_height:
            self.chat_js.set_style_property_element('#chat', 'position', 'relative')
            self.chat_js.set_style_property_element('#chat', 'top', '%dpx' % (widget_height - content_height))
        else:
            self.chat_js.set_style_property_element('#chat', 'position', 'static')
            self.chat_js.set_style_property_element('#chat', 'top', None)
        if scroll:
            self.chat_js.scroll_to_bottom()

    def _align_style_preview(self, scroll=False):
        content_height = self.chat_element.geometry().height()
        self._process_height(content_height, scroll=scroll)

    # Signal handlers
    #
    def _SH_ToolbarActionTriggered(self, action):
        if action == self.logging_action:
            self._update_logs_size_label()
        self.pages.setCurrentIndex(action.index)

    def _NH_SIPRegistrationInfoDidChange(self, notification):
        self.refresh_account_registration_widgets(notification.sender)

    def refresh_account_registration_widgets(self, account):
        try:
            selected_index = self.account_list.selectionModel().selectedIndexes()[0]
        except IndexError:
            return

        selected_account = selected_index.data(Qt.ItemDataRole.UserRole).account
        if account.id != selected_account.id:
            return

        self.load_account_settings(selected_account)

    def _SH_AccountListSelectionChanged(self, selected, deselected):
        try:
            selected_index = self.account_list.selectionModel().selectedIndexes()[0]
        except IndexError:
            self.delete_account_button.setEnabled(False)
            self.account_tab_widget.setEnabled(False)
        else:
            selected_account = selected_index.data(Qt.ItemDataRole.UserRole).account
            self.delete_account_button.setEnabled(selected_account is not BonjourAccount())
            tab_widget = self.account_tab_widget
            tab_widget.setEnabled(True)
            if selected_account is BonjourAccount():
                tab_widget.removeTab(tab_widget.indexOf(self.server_settings_tab))
                tab_widget.removeTab(tab_widget.indexOf(self.network_tab))
                tab_widget.removeTab(tab_widget.indexOf(self.advanced_tab))
                self.password_label.hide()
                self.password_editor.hide()
            else:
                if tab_widget.indexOf(self.server_settings_tab) == -1:
                    tab_widget.addTab(self.server_settings_tab, translate('preferences_window', "Server Settings"))
                if tab_widget.indexOf(self.network_tab) == -1:
                    tab_widget.addTab(self.network_tab, translate('preferences_window', "NAT Traversal"))
                if tab_widget.indexOf(self.advanced_tab) == -1:
                    tab_widget.addTab(self.advanced_tab, translate('preferences_window', "Advanced"))
                self.password_label.show()
                self.password_editor.show()
                self.voicemail_uri_editor.inactiveText = translate('preferences_window', "Discovered by subscribing to %s") % selected_account.id
                self.xcap_root_editor.inactiveText = translate('preferences_window', "Taken from the DNS TXT record for xcap.%s") % selected_account.id.domain
            self.load_account_settings(selected_account)

    def _SH_AccountListDataChanged(self, topLeft, bottomRight):
        try:
            selected_index = self.account_list.selectionModel().selectedIndexes()[0]
        except IndexError:
            pass
        else:
            account_info = self.account_list.model().data(topLeft, Qt.ItemDataRole.UserRole)
            selected_account_info = self.account_list.model().data(selected_index, Qt.ItemDataRole.UserRole)
            if selected_account_info is account_info:
                if account_info.registration_state:
                    self.account_registration_label.setText(translate('preferences_window', 'Registration %s') % account_info.registration_state.title())
                else:
                    self.account_registration_label.setText(translate('preferences_window', 'Not Registered'))

    def _SH_DeleteAccountButtonClicked(self):
        model = self.account_list.model()

        selected_index = self.account_list.selectionModel().selectedIndexes()[0]
        selected_account = selected_index.data(Qt.ItemDataRole.UserRole).account

        title, message = translate('preferences_window', "Remove Account"), translate('preferences_window', "Permanently remove account %s?") % selected_account.id
        if QMessageBox.question(self, title, message, QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel) == QMessageBox.StandardButton.Cancel:
            return

        account_manager = AccountManager()
        if account_manager.default_account is selected_account:
            active_accounts = [account_info.account for account_info in model.accounts if account_info.account.enabled]
            position = active_accounts.index(selected_account)
            if position < len(active_accounts) - 1:
                account_manager.default_account = active_accounts[position + 1]
            elif position > 0:
                account_manager.default_account = active_accounts[position - 1]
            else:
                account_manager.default_account = None

        selected_account.delete()

    # Account information
    def _SH_AccountEnabledButtonClicked(self, checked):
        account = self.selected_account
        account.enabled = checked
        account.save()

    def _SH_AccountEnabledPresenceButtonClicked(self, checked):
        account = self.selected_account
        account.presence.enabled = checked
        account.save()

    def _SH_AccountEnabledMWIButtonClicked(self, checked):
        account = self.selected_account
        account.message_summary.enabled = checked
        account.save()

    def _SH_DisplayNameEditorEditingFinished(self):
        account = self.selected_account
        display_name = self.display_name_editor.text() or None
        if account.display_name != display_name:
            account.display_name = display_name
            account.save()

    def _SH_PasswordEditorEditingFinished(self):
        account = self.selected_account
        password = self.password_editor.text()
        if account.auth.password != password:
            account.auth.password = password
            account.save()

    # Account media settings
    def _SH_AccountAudioCodecsListItemChanged(self, item):
        account = self.selected_account
        items = [self.account_audio_codecs_list.item(row) for row in range(self.account_audio_codecs_list.count())]
        account.rtp.audio_codec_list = [item.text() for item in items if item.checkState() == Qt.CheckState.Checked]
        account.rtp.audio_codec_order = [item.text() for item in items]
        account.save()

    def _SH_AccountAudioCodecsListModelRowsMoved(self, source_parent, source_start, source_end, dest_parent, dest_row):
        account = self.selected_account
        items = [self.account_audio_codecs_list.item(row) for row in range(self.account_audio_codecs_list.count())]
        account.rtp.audio_codec_list = [item.text() for item in items if item.checkState() == Qt.CheckState.Checked]
        account.rtp.audio_codec_order = [item.text() for item in items]
        account.save()

    def _SH_ResetAudioCodecsButtonClicked(self, checked):
        settings = SIPSimpleSettings()
        account = self.selected_account

        with blocked_qt_signals(self.account_audio_codecs_list):
            self.account_audio_codecs_list.clear()
            audio_codec_order = settings.rtp.audio_codec_order
            audio_codec_list = settings.rtp.audio_codec_list
            for codec in audio_codec_order:
                item = QListWidgetItem(codec, self.account_audio_codecs_list)
                item.setCheckState(Qt.CheckState.Checked if codec in audio_codec_list else Qt.CheckState.Unchecked)

        account.rtp.audio_codec_list  = DefaultValue
        account.rtp.audio_codec_order = DefaultValue
        account.save()

    def _SH_AccountVideoCodecsListItemChanged(self, item):
        account = self.selected_account
        items = [self.account_video_codecs_list.item(row) for row in range(self.account_video_codecs_list.count())]
        account.rtp.video_codec_list = [item.text() for item in items if item.checkState() == Qt.CheckState.Checked]
        account.rtp.video_codec_order = [item.text() for item in items]
        account.save()

    def _SH_AccountVideoCodecsListModelRowsMoved(self, source_parent, source_start, source_end, dest_parent, dest_row):
        account = self.selected_account
        items = [self.account_video_codecs_list.item(row) for row in range(self.account_video_codecs_list.count())]
        account.rtp.video_codec_list = [item.text() for item in items if item.checkState() == Qt.CheckState.Checked]
        account.rtp.video_codec_order = [item.text() for item in items]
        account.save()

    def _SH_ResetVideoCodecsButtonClicked(self, checked):
        settings = SIPSimpleSettings()
        account = self.selected_account

        with blocked_qt_signals(self.account_video_codecs_list):
            self.account_video_codecs_list.clear()
            video_codec_order = settings.rtp.video_codec_order
            video_codec_list = settings.rtp.video_codec_list
            for codec in video_codec_order:
                item = QListWidgetItem(codec, self.account_video_codecs_list)
                item.setCheckState(Qt.CheckState.Checked if codec in video_codec_list else Qt.CheckState.Unchecked)

        account.rtp.video_codec_list  = DefaultValue
        account.rtp.video_codec_order = DefaultValue
        account.save()

    def _SH_InbandDTMFButtonClicked(self, checked):
        account = self.selected_account
        account.rtp.inband_dtmf = checked
        account.save()

    def _SH_RTPEncryptionButtonClicked(self, checked):
        self.key_negotiation_button.setEnabled(checked)
        account = self.selected_account
        account.rtp.encryption.enabled = checked
        account.save()

    def _SH_AutoAnswerIntervalChanged(self, interval):
        settings = SIPSimpleSettings()
        settings.sip.auto_answer_interval = interval
        settings.save()

    def _SH_AccountAutoAnswerChanged(self, auto_answer):
        account = self.selected_account
        account.sip.auto_answer = not account.sip.auto_answer
        account.save()

    def _SH_KeyNegotiationButtonActivated(self, index):
        account = self.selected_account
        account.rtp.encryption.key_negotiation = self.key_negotiation_button.itemData(index)
        account.save()

    # Account server settings
    def _SH_AlwaysUseMyProxyButtonClicked(self, checked):
        account = self.selected_account
        account.sip.always_use_my_proxy = checked
        account.save()

    def _SH_OutboundProxyHostEditorEditingFinished(self):
        account = self.selected_account
        outbound_proxy = self.account_outbound_proxy
        if account.sip.outbound_proxy != outbound_proxy:
            account.sip.outbound_proxy = outbound_proxy
            account.save()

    def _SH_OutboundProxyPortValueChanged(self, value):
        account = self.selected_account
        outbound_proxy = self.account_outbound_proxy
        if account.sip.outbound_proxy != outbound_proxy:
            account.sip.outbound_proxy = outbound_proxy
            account.save()

    def _SH_OutboundProxyTransportButtonActivated(self, index):
        account = self.selected_account
        outbound_proxy = self.account_outbound_proxy
        if account.sip.outbound_proxy != outbound_proxy:
            account.sip.outbound_proxy = outbound_proxy
            account.save()

    def _SH_AuthUsernameEditorEditingFinished(self):
        account = self.selected_account
        auth_username = self.auth_username_editor.text() or None
        if account.auth.username != auth_username:
            account.auth.username = auth_username
            account.save()

    def _SH_TLSPeerNameEditorEditingFinished(self):
        account = self.selected_account
        tls_name = self.account_tls_name_editor.text() or None
        if account.sip.tls_name != tls_name:
            account.sip.tls_name = tls_name
            account.save()

    def _SH_AlwaysUseMyMSRPRelayButtonClicked(self, checked):
        account = self.selected_account
        account.nat_traversal.use_msrp_relay_for_outbound = checked
        account.save()

    def _SH_MSRPRelayHostEditorEditingFinished(self):
        account = self.selected_account
        msrp_relay = self.account_msrp_relay
        if account.nat_traversal.msrp_relay != msrp_relay:
            account.nat_traversal.msrp_relay = msrp_relay
            account.save()

    def _SH_StunServerListEditorEditingFinished(self):
        account = self.selected_account
        stun_server_list = self.stun_server_list_editor.text().strip().lower() or ''
        new_stun_server_list = []
        if stun_server_list:
            for server in stun_server_list.split(","):
                try:
                    (host, port) = server.strip().split(':')
                except ValueError:
                    host = server
                    port = STUNServerAddress.default_port
                else:
                    try:
                        int(port)
                    except (TypeError, ValueError) as e:
                        port = STUNServerAddress.default_port

                try:
                    new_stun_server_list.append(STUNServerAddress(host, port))
                except ValueError as e:
                    continue

        new_stun_server_list = new_stun_server_list or None

        if account.nat_traversal.stun_server_list != new_stun_server_list:
            try:
                account.nat_traversal.stun_server_list = new_stun_server_list
                account.save()
            except ValueError as e:
                pass

    def _SH_MSRPRelayPortValueChanged(self, index):
        account = self.selected_account
        msrp_relay = self.account_msrp_relay
        if account.nat_traversal.msrp_relay != msrp_relay:
            account.nat_traversal.msrp_relay = msrp_relay
            account.save()

    def _SH_MSRPRelayTransportButtonActivated(self, index):
        account = self.selected_account
        msrp_relay = self.account_msrp_relay
        if account.nat_traversal.msrp_relay != msrp_relay:
            account.nat_traversal.msrp_relay = msrp_relay
            account.save()

    def _SH_VoicemailURIEditorEditingFinished(self):
        account = self.selected_account
        voicemail_uri = self.voicemail_uri_editor.text() or None
        if account.message_summary.voicemail_uri != voicemail_uri:
            account.message_summary.voicemail_uri = voicemail_uri
            account.save()

    def _SH_EnableXcapButtonClicked(self, checked):
        account = self.selected_account
        account.xcap.enabled = checked
        self.xcap_root_editor.setEnabled(checked)
        account.save()

    def _SH_XCAPRootEditorEditingFinished(self):
        account = self.selected_account
        xcap_root = self.xcap_root_editor.text() or None
        if account.xcap.xcap_root != xcap_root:
            account.xcap.xcap_root = xcap_root
            account.save()

    def _SH_ServerToolsURLEditorEditingFinished(self):
        account = self.selected_account
        url = self.server_tools_url_editor.text() or None
        if account.server.settings_url != url:
            account.server.settings_url = url
            account.save()

    def _SH_ConferenceServerEditorEditingFinished(self):
        account = self.selected_account
        server = self.conference_server_editor.text() or None
        if account.server.conference_server != server:
            account.server.conference_server = server
            account.save()

    # Account network settings
    def _SH_UseICEButtonClicked(self, checked):
        account = self.selected_account
        account.nat_traversal.use_ice = checked
        account.save()

    def _SH_MSRPTransportButtonActivated(self, index):
        text = self.msrp_transport_button.itemText(index)
        account = self.selected_account
        account.msrp.transport = text.lower()
        account.save()

    # Account advanced settings
    def _SH_RegisterIntervalValueChanged(self, value):
        account = self.selected_account
        account.sip.register_interval = value
        account.save()

    def _SH_PublishIntervalValueChanged(self, value):
        account = self.selected_account
        account.sip.publish_interval = value
        account.save()

    def _SH_SubscribeIntervalValueChanged(self, value):
        account = self.selected_account
        account.sip.subscribe_interval = value
        account.save()

    def _SH_ReregisterButtonClicked(self):
        account = self.selected_account
        account.reregister()

    def _SH_IDDPrefixButtonActivated(self, index):
        text = self.idd_prefix_button.itemText(index)
        self._update_pstn_example_label()
        account = self.selected_account
        idd_prefix = None if text == '+' else text
        if account.pstn.idd_prefix != idd_prefix:
            account.pstn.idd_prefix = idd_prefix
            account.save()

    def _SH_PrefixButtonActivated(self, index):
        text = self.idd_prefix_button.itemText(index)
        self._update_pstn_example_label()
        account = self.selected_account
        prefix = None if text == 'None' else text
        if account.pstn.prefix != prefix:
            account.pstn.prefix = prefix
            account.save()

    def _SH_TLSCertFileEditorLocationCleared(self):
        settings = SIPSimpleSettings()
        settings.tls.certificate = None
        settings.save()

    def _SH_TLSCertFileBrowseButtonClicked(self, checked):
        # TODO: open the file selection dialog in non-modal mode (and the error messages boxes as well). -Dan
        settings = SIPSimpleSettings()
        directory = os.path.dirname(settings.tls.certificate.normalized) if settings.tls.certificate else Path('~').normalized
        cert_path = QFileDialog.getOpenFileName(self, 'Select Certificate File', directory, "TLS certificates (*.crt *.pem)")[0] or None
        if cert_path is not None:
            cert_path = os.path.normpath(cert_path)
            if cert_path != settings.tls.certificate:
                try:
                    contents = open(cert_path).read()
                    X509Certificate(contents)
                    X509PrivateKey(contents)
                except (OSError, IOError) as e:
                    QMessageBox.critical(self, translate('preferences_window', "TLS Certificate Error"), translate('preferences_window', "The certificate file could not be opened: %s") % e.strerror)
                except GNUTLSError as e:
                    QMessageBox.critical(self, translate('preferences_window', "TLS Certificate Error"), translate('preferences_window', "The certificate file is invalid: %s") % e)
                else:
                    self.tls_cert_file_editor.setText(cert_path)
                    settings.tls.certificate = cert_path
                    settings.save()

    def _SH_TLSVerifyServerButtonClicked(self, checked):
        settings = SIPSimpleSettings()
        settings.tls.verify_server = checked
        settings.save()

    def _SH_EnableMessageCPIMButtonClicked(self, checked):
        account = self.selected_account
        account.sms.use_cpim = checked
        self.message_imdn_enabled_button.setEnabled(account.sms.use_cpim)
        account.save()

    def _SH_EnableMessageIsComposingButtonClicked(self, checked):
        account = self.selected_account
        account.sms.enable_iscomposing = checked
        account.save()

    def _SH_EnableMessageIMDNButtonClicked(self, checked):
        account = self.selected_account
        account.sms.enable_imdn = checked
        account.save()

    def _SH_AddUnknownContactsButtonClicked(self, checked):
        account = self.selected_account
        account.sms.add_unknown_contacts = checked
        account.save()

    def _SH_EnablePGPButtonClicked(self, checked):
        account = self.selected_account
        account.sms.enable_pgp = checked
        account.save()

    def _SH_MessageReplicationButtonClicked(self, checked):
        account = self.selected_account
        account.sms.enable_message_replication = checked
        account.save()

    def _SH_MessageSynchronizationButtonClicked(self, checked):
        account = self.selected_account
        account.sms.enable_history_synchronization = checked
        account.save()

    def _SH_HistoryUrlEditorEditingFinshed(self):
        account = self.selected_account
        history_url = self.history_url_editor.text() or None
        if account.sms.history_synchronization_url != history_url:
            account.sms.history_synchronization_url = history_url
            account.save()

    def _SH_LastIdEditorEditingFinished(self):
        account = self.selected_account
        last_id = self.last_id_editor.text() or None
        if account.sms.history_synchronization_id != last_id:
            account.sms.history_synchronization_id = last_id
            account.save()

    # Audio devices signal handlers
    def _SH_AudioAlertDeviceButtonActivated(self, index):
        device = self.audio_alert_device_button.itemData(index)
        settings = SIPSimpleSettings()
        settings.audio.alert_device = device
        settings.save()

    def _SH_AudioInputDeviceButtonActivated(self, index):
        device = self.audio_input_device_button.itemData(index)
        settings = SIPSimpleSettings()
        settings.audio.input_device = device
        settings.save()

    def _SH_AudioOutputDeviceButtonActivated(self, index):
        device = self.audio_output_device_button.itemData(index)
        settings = SIPSimpleSettings()
        settings.audio.output_device = device
        settings.save()

    def _SH_AudioSampleRateButtonActivated(self, index):
        text = self.audio_sample_rate_button.itemData(index)
        settings = SIPSimpleSettings()
        settings.audio.sample_rate = text
        settings.save()

    def _SH_EnableEchoCancellingButtonClicked(self, checked):
        settings = SIPSimpleSettings()
        settings.audio.echo_canceller.enabled = checked
        settings.save()

    def _SH_TailLengthSliderValueChanged(self, value):
        settings = SIPSimpleSettings()
        settings.audio.echo_canceller.tail_length = value
        settings.save()

    # Audio codecs signal handlers
    def _SH_AudioCodecsListItemChanged(self, item):
        settings = SIPSimpleSettings()
        item_iterator = (self.audio_codecs_list.item(row) for row in range(self.audio_codecs_list.count()))
        settings.rtp.audio_codec_list = [item.text() for item in item_iterator if item.checkState() == Qt.CheckState.Checked]
        settings.save()

    def _SH_AudioCodecsListModelRowsMoved(self, source_parent, source_start, source_end, dest_parent, dest_row):
        settings = SIPSimpleSettings()
        items = [self.audio_codecs_list.item(row) for row in range(self.audio_codecs_list.count())]
        settings.rtp.audio_codec_order = [item.text() for item in items]
        settings.rtp.audio_codec_list = [item.text() for item in items if item.checkState() == Qt.CheckState.Checked]
        settings.save()

    # Answering machine signal handlers
    def _SH_EnableAnsweringMachineButtonClicked(self, checked):
        settings = SIPSimpleSettings()
        settings.answering_machine.enabled = checked
        settings.save()

    def _SH_AnswerDelayValueChanged(self, value):
        if value == 0:
            self.answer_delay_seconds_label.setText('')
        elif value == 1:
            self.answer_delay_seconds_label.setText(translate('preferences_window', 'second'))
        else:
            self.answer_delay_seconds_label.setText(translate('preferences_window', 'seconds'))
        settings = SIPSimpleSettings()
        if settings.answering_machine.answer_delay != value:
            settings.answering_machine.answer_delay = value
            settings.save()

    def _SH_MaxRecordingValueChanged(self, value):
        self.max_recording_minutes_label.setText(translate('preferences_window', 'minute') if value == 1 else translate('preferences_window', 'minutes'))
        settings = SIPSimpleSettings()
        if settings.answering_machine.max_recording != value:
            settings.answering_machine.max_recording = value
            settings.save()

    # Video devices signal handlers
    def _SH_VideoCameraButtonActivated(self, index):
        device = self.video_camera_button.itemData(index)
        settings = SIPSimpleSettings()
        settings.video.device = device
        settings.save()

    def _SH_VideoResolutionButtonActivated(self, index):
        resolution = self.video_resolution_button.itemData(index)
        settings = SIPSimpleSettings()
        settings.video.resolution = resolution
        settings.video.h264.level = self.h264_level_map[resolution]
        settings.save()

    def _SH_VideoFramerateButtonActivated(self, index):
        framerate = self.video_framerate_button.itemData(index)
        settings = SIPSimpleSettings()
        settings.video.framerate = framerate
        settings.save()

    # Video codecs signal handlers
    def _SH_VideoCodecsListItemChanged(self, item):
        settings = SIPSimpleSettings()
        item_iterator = (self.video_codecs_list.item(row) for row in range(self.video_codecs_list.count()))
        settings.rtp.video_codec_list = [item.text() for item in item_iterator if item.checkState() == Qt.CheckState.Checked]
        settings.save()

    def _SH_VideoCodecsListModelRowsMoved(self, source_parent, source_start, source_end, dest_parent, dest_row):
        settings = SIPSimpleSettings()
        items = [self.video_codecs_list.item(row) for row in range(self.video_codecs_list.count())]
        settings.rtp.video_codec_order = [item.text() for item in items]
        settings.rtp.video_codec_list = [item.text() for item in items if item.checkState() == Qt.CheckState.Checked]
        settings.save()

    def _SH_VideoCodecBitrateButtonActivated(self, index):
        bitrate = self.video_codec_bitrate_button.itemData(index)
        settings = SIPSimpleSettings()
        settings.video.max_bitrate = bitrate
        settings.save()

    def _SH_H264ProfileButtonActivated(self, index):
        profile = self.h264_profile_button.itemData(index)
        settings = SIPSimpleSettings()
        settings.video.h264.profile = profile
        settings.save()

    # Chat and SMS signal handlers
    def _SH_StyleViewSizeChanged(self):
        self._align_style_preview(scroll=True)

    def _SH_StyleViewFrameContentsSizeChanged(self, size):
        self._align_style_preview(scroll=True)

    def _SH_StyleButtonActivated(self, index):
        style = self.style_button.itemData(index)
        settings = BlinkSettings()
        if style.name != settings.chat_window.style:
            self.style_variant_button.clear()
            for variant in style.variants:
                self.style_variant_button.addItem(variant)
            self.style_variant_button.setCurrentIndex(self.style_variant_button.findText(style.default_variant))
            settings.chat_window.style = style.name
            settings.chat_window.style_variant = None
            settings.save()

    def _SH_StyleVariantButtonActivated(self, index):
        style = self.style_button.itemData(self.style_button.currentIndex())
        style_variant = self.style_variant_button.itemText(index)
        settings = BlinkSettings()
        current_variant = settings.chat_window.style_variant or style.default_variant
        if style_variant != current_variant:
            settings.chat_window.style_variant = style_variant
            settings.save()

    def _SH_StyleShowIconsButtonClicked(self, checked):
        settings = BlinkSettings()
        settings.chat_window.show_user_icons = checked
        settings.save()

    def _SH_StyleFontButtonCurrentIndexChanged(self, index):
        font = self.style_font_button.itemText(index)
        settings = BlinkSettings()
        settings.chat_window.font = font
        settings.save()

    def _SH_StyleFontSizeValueChanged(self, size):
        settings = BlinkSettings()
        settings.chat_window.font_size = size
        settings.save()

    def _SH_StyleDefaultFontButtonClicked(self, checked):
        settings = BlinkSettings()
        settings.chat_window.font = DefaultValue
        settings.chat_window.font_size = DefaultValue
        settings.save()
        style = self.style_button.itemData(self.style_button.currentIndex())
        with blocked_qt_signals(self.style_font_button):
            self.style_font_button.setCurrentFont(QFont(style.font_family))
        with blocked_qt_signals(self.style_font_size):
            self.style_font_size.setValue(style.font_size)

    def _SH_AutoAcceptChatButtonClicked(self, checked):
        settings = SIPSimpleSettings()
        settings.chat.auto_accept = checked
        settings.save()

    def _SH_ChatMessageAlertButtonClicked(self, checked):
        settings = SIPSimpleSettings()
        settings.sounds.play_message_alerts = checked
        settings.save()

    def _SH_SMSReplicationButtonClicked(self, checked):
        settings = SIPSimpleSettings()
        settings.chat.sms_replication = checked
        settings.save()

    def _SH_SessionInfoStyleButtonClicked(self, checked):
        settings = BlinkSettings()
        settings.chat_window.session_info.alternate_style = checked
        settings.save()

    def _SH_TrafficUnitsButtonClicked(self, checked):
        settings = BlinkSettings()
        settings.chat_window.session_info.bytes_per_second = checked
        settings.save()

    # Screen sharing signal handlers
    def _SH_ScreenshotsDirectoryBrowseButtonClicked(self, checked):
        # TODO: open the file selection dialog in non-modal mode. Same for the one for TLS CA list and the IconSelector from contacts. -Dan
        settings = BlinkSettings()
        directory = QFileDialog.getExistingDirectory(self, translate('preferences_window', 'Select Screenshots Directory'), settings.screenshots_directory.normalized) or None
        if directory is not None:
            directory = os.path.normpath(directory)
            if directory != settings.screenshots_directory:
                self.screenshots_directory_editor.setText(directory)
                settings.screenshots_directory = directory
                settings.save()

    def _SH_ScreenSharingScaleButtonClicked(self, checked):
        settings = BlinkSettings()
        settings.screen_sharing.scale = checked
        settings.save()

    def _SH_ScreenSharingFullscreenButtonClicked(self, checked):
        settings = BlinkSettings()
        settings.screen_sharing.open_fullscreen = checked
        settings.save()

    def _SH_ScreenSharingViewonlyButtonClicked(self, checked):
        settings = BlinkSettings()
        settings.screen_sharing.open_viewonly = checked
        settings.save()

    # File transfer signal handlers
    def _SH_TransfersDirectoryBrowseButtonClicked(self, checked):
        # TODO: open the file selection dialog in non-modal mode. Same for the one for TLS CA list and the IconSelector from contacts. -Dan
        settings = BlinkSettings()
        directory = QFileDialog.getExistingDirectory(self, translate('preferences_window', 'Select Transfers Directory'), settings.transfers_directory.normalized) or None
        if directory is not None:
            directory = os.path.normpath(directory)
            if directory != settings.transfers_directory:
                self.transfers_directory_editor.setText(directory)
                settings.transfers_directory = directory
                settings.save()

    # File logging signal handlers
    def _SH_TraceSIPButtonClicked(self, checked):
        settings = SIPSimpleSettings()
        settings.logs.trace_sip = checked
        settings.save()

    def _SH_TraceMessagingButtonClicked(self, checked):
        settings = SIPSimpleSettings()
        settings.logs.trace_messaging = checked
        settings.save()

    def _SH_TraceMSRPButtonClicked(self, checked):
        settings = SIPSimpleSettings()
        settings.logs.trace_msrp = checked
        settings.save()

    def _SH_TraceXCAPButtonClicked(self, checked):
        settings = SIPSimpleSettings()
        settings.logs.trace_xcap = checked
        settings.save()

    def _SH_TraceNotificationsButtonClicked(self, checked):
        settings = SIPSimpleSettings()
        settings.logs.trace_notifications = checked
        settings.save()

    def _SH_TracePJSIPButtonClicked(self, checked):
        settings = SIPSimpleSettings()
        settings.logs.trace_pjsip = checked
        settings.save()

    def _SH_PJSIPTraceLevelValueChanged(self, value):
        settings = SIPSimpleSettings()
        if settings.logs.pjsip_level != value:
            settings.logs.pjsip_level = value
            settings.save()

    @run_in_thread('file-io')
    def _SH_ClearLogFilesButtonClicked(self):
        log_manager = LogManager()
        log_manager.stop()
        for path, dirs, files in os.walk(os.path.join(ApplicationData.directory, 'logs'), topdown=False):
            for name in files:
                try:
                    os.remove(os.path.join(path, name))
                except (OSError, IOError):
                    pass
            for name in dirs:
                try:
                    os.rmdir(os.path.join(path, name))
                except (OSError, IOError):
                    pass
        log_manager.start()
        call_in_gui_thread(self._update_logs_size_label)

    # SIP and RTP signal handlers
    def _SH_SIPTransportsButtonClicked(self, button):
        settings = SIPSimpleSettings()
        settings.sip.transport_list = [button.name for button in self.sip_transports_button_group.buttons() if button.isChecked()]
        settings.save()

    def _SH_UDPPortValueChanged(self, value):
        settings = SIPSimpleSettings()
        if settings.sip.udp_port != value:
            settings.sip.udp_port = value
            settings.save()

    def _SH_TCPPortValueChanged(self, value):
        settings = SIPSimpleSettings()
        if settings.sip.tcp_port != value:
            settings.sip.tcp_port = value
            settings.save()

    def _SH_TLSPortValueChanged(self, value):
        settings = SIPSimpleSettings()
        if settings.sip.tls_port != value:
            settings.sip.tls_port = value
            settings.save()

    def _SH_MediaPortsStartValueChanged(self, value):
        self.media_ports.setMaximum(limit(65535-value, min=10, max=10000))
        settings = SIPSimpleSettings()
        port_range = PortRange(value, value + self.media_ports.value())
        if settings.rtp.port_range != port_range:
            settings.rtp.port_range = port_range
            settings.save()

    def _SH_MediaPortsValueChanged(self, value):
        self.media_ports_start.setMaximum(limit(65535-value, min=10000, max=65000))
        settings = SIPSimpleSettings()
        port_range = PortRange(self.media_ports_start.value(), self.media_ports_start.value() + value)
        if settings.rtp.port_range != port_range:
            settings.rtp.port_range = port_range
            settings.save()

    # TLS signal handlers
    def _SH_TLSCAFileEditorLocationCleared(self):
        settings = SIPSimpleSettings()
        settings.tls.ca_list = None
        settings.save()

    def _SH_TLSCAFileBrowseButtonClicked(self):
        # TODO: open the file selection dialog in non-modal mode (and the error messages boxes as well). -Dan
        settings = SIPSimpleSettings()
        directory = os.path.dirname(settings.tls.ca_list.normalized) if settings.tls.ca_list else Path('~').normalized
        ca_path = QFileDialog.getOpenFileName(self, 'Select Certificate Authority File', directory, "TLS certificates (*.crt *.pem)")[0] or None
        if ca_path is not None:
            ca_path = os.path.normpath(ca_path)
            if ca_path != settings.tls.ca_list:
                try:
                    X509Certificate(open(ca_path).read())
                except (OSError, IOError) as e:
                    QMessageBox.critical(self, translate('preferences_window', "TLS Certificate Error"), translate('preferences_window', "The certificate authority file could not be opened: %s") % e.strerror)
                except GNUTLSError as e:
                    QMessageBox.critical(self, translate('preferences_window', "TLS Certificate Error"), translate('preferences_window', "The certificate authority file is invalid: %s") % e)
                else:
                    self.tls_ca_file_editor.setText(ca_path)
                    settings.tls.ca_list = ca_path
                    settings.save()

    def _SH_HistoryNameAndUriButtonClicked(self, checked):
        settings = BlinkSettings()
        settings.interface.show_history_name_and_uri = checked
        settings.save()

    def _SH_LanguageButtonActivated(self, index):
        data = self.language_button.itemData(index)
        settings = BlinkSettings()
        if data.language_code != settings.interface.language:
            settings.interface.language = data.language_code
            settings.save()
            title = translate('preferences_window', "Restart required")
            question = translate('preferences_window', "The application language was changed. A restart is required to apply the change. Would you like to restart now?")
            if QMessageBox.question(self, title, question) == QMessageBox.StandardButton.No:
                return

            blink = QApplication.instance()
            blink.restart()

    def _SH_ShowMessagesGroupButtonClicked(self, checked):
        settings = BlinkSettings()
        settings.interface.show_messages_group = checked
        settings.save()

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPApplicationDidStart(self, notification):
        self._sync_defaults()
        self.load_settings()
        notification.center.add_observer(self, name='AudioDevicesDidChange')
        notification.center.add_observer(self, name='VideoDevicesDidChange')
        notification.center.add_observer(self, name='VideoDeviceDidChangeCamera')
        notification.center.add_observer(self, name='CFGSettingsObjectDidChange')
        notification.center.add_observer(self, name='SIPRegistrationInfoDidChange')

    def _NH_AudioDevicesDidChange(self, notification):
        self.load_audio_devices()

    def _NH_VideoDevicesDidChange(self, notification):
        self.load_video_devices()

    def _NH_VideoDeviceDidChangeCamera(self, notification):
        if self.camera_preview.isVisible():
            self.camera_preview.producer = SIPApplication.video_device.producer

    def _NH_CFGSettingsObjectDidChange(self, notification):
        settings = SIPSimpleSettings()
        blink_settings = BlinkSettings()
        if notification.sender is blink_settings:
            if {'chat_window.style', 'chat_window.style_variant', 'chat_window.show_user_icons'}.intersection(notification.data.modified):
                self.update_chat_preview()
            if {'chat_window.font', 'chat_window.font_size'}.intersection(notification.data.modified):
                self.update_chat_preview()
                self.style_default_font_button.setEnabled(blink_settings.chat_window.font is not None or blink_settings.chat_window.font_size is not None)
        elif notification.sender is settings:
            if 'audio.alert_device' in notification.data.modified:
                self.audio_alert_device_button.setCurrentIndex(self.audio_alert_device_button.findData(settings.audio.alert_device))
            if 'audio.input_device' in notification.data.modified:
                self.audio_input_device_button.setCurrentIndex(self.audio_input_device_button.findData(settings.audio.input_device))
            if 'audio.output_device' in notification.data.modified:
                self.audio_output_device_button.setCurrentIndex(self.audio_output_device_button.findData(settings.audio.output_device))
            if 'answering_machine.enabled' in notification.data.modified:
                self.enable_answering_machine_button.setChecked(settings.answering_machine.enabled)
            if 'chat.auto_accept' in notification.data.modified:
                self.auto_accept_chat_button.setChecked(settings.chat.auto_accept)
            if 'sounds.play_message_alerts' in notification.data.modified:
                self.chat_message_alert_button.setChecked(settings.sounds.play_message_alerts)
            if 'sip.auto_answer_interval' in notification.data.modified:
                self.auto_answer_interval.setValue(settings.sip.auto_answer_interval)
            if 'video.device' in notification.data.modified:
                self.video_camera_button.setCurrentIndex(self.video_camera_button.findData(settings.video.device))
        elif notification.sender is self.selected_account is not None:
            account = notification.sender
            if 'sip.auto_answer' in notification.data.modified:
                self.account_auto_answer.setChecked(account.sip.auto_answer)
            if 'enabled' in notification.data.modified:
                self.account_enabled_button.setChecked(account.enabled)
                if not account.enabled:
                    self.refresh_account_registration_widgets(account)
                self.reregister_button.setEnabled(account.enabled)
            if 'message_summary.enabled' in notification.data.modified:
                self.account_enabled_mwi_button.setChecked(account.message_summary.enabled)
            if 'presence.enabled' in notification.data.modified:
                self.account_enabled_presence_button.setChecked(account.presence.enabled)
            if 'display_name' in notification.data.modified:
                self.display_name_editor.setText(account.display_name or '')
            if 'rtp.audio_codec_list' in notification.data.modified:
                self.reset_account_audio_codecs_button.setEnabled(account.rtp.audio_codec_list is not None)
            if 'rtp.video_codec_list' in notification.data.modified:
                self.reset_account_video_codecs_button.setEnabled(account.rtp.video_codec_list is not None)
            if 'sms.history_synchronization_url' in notification.data.modified:
                if not account.sms.history_synchronization_url:
                    account.sms.history_synchronization_token = None
                    account.sms.enable_history_synchronization = False
                    account.save()
                else:
                    self.history_url_editor.setText(account.sms.history_synchronization_url)
            if 'sms.history_synchronization_token' in notification.data.modified:
                if account.sms.history_synchronization_token:
                    self.last_id_editor.setText(account.sms.history_synchronization_id)


del ui_class, base_class
