
from PyQt5 import uic
from PyQt5.QtCore import Qt

from blink import __date__, __version__
from blink.resources import Resources
from blink.util import QSingleton, translate


__all__ = ['AboutPanel']


credits_text = """
<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.0//EN" "http://www.w3.org/TR/REC-html40/strict.dtd">
<html>
<head>
<meta name="qrichtext" content="1" />
<style type="text/css">
 td.name { text-align: right; padding-right: 6px; }
 a:link  { text-decoration: none; color: #1f487f; }
</style>
</head>
<body>
<a href="http://nlnet.nl/">NLnet Foundation</a>
<p>
Software authors:
<p>

<ul>
 <li>Dan Pascu</li>
 <li>Lucian Stănescu</li>
 <li>Adrian Georgescu</li>
 <li>Saúl Ibarra Corretgé</li>
 <li>Tijmen de Mes</li>
</ul>
</body>
</html>
"""

ui_class, base_class = uic.loadUiType(Resources.get('about_panel.ui'))


class AboutPanel(base_class, ui_class, metaclass=QSingleton):
    def __init__(self, parent=None):
        super(AboutPanel, self).__init__(parent)

        with Resources.directory:
            self.setupUi(self)

        self.version.setText(translate('about_panel', 'Version %s\n%s') % (__version__, __date__))

        credits_width = self.credits_text.fontMetrics().size(Qt.TextFlag.TextSingleLine, "NLnet Foundation" + "http://sipsimpleclient.org").width() + 40
        self.credits_text.setFixedWidth(credits_width)
        self.credits_text.document().documentLayout().documentSizeChanged.connect(self._credits_size_changed)
        self.credits_text.setHtml(credits_text)

    def _credits_size_changed(self, size):
        self.credits_text.document().documentLayout().documentSizeChanged.disconnect(self._credits_size_changed)
        self.setFixedSize(self.minimumSize().width(), int(self.minimumSize().width() * 1.40))  # set a fixed aspect ratio
        row_height = self.credits_text.fontMetrics().height() + 2  # +2 for cellspacing
        max_credits_height = 8 * row_height + 2 + 14  # allow for maximum 8 rows; +2 for cellspacing and +14 for top/bottom margins
        if self.credits_text.height() > max_credits_height:
            self.setFixedHeight(self.height() - (self.credits_text.height() - max_credits_height))


del ui_class, base_class
