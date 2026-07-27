"""Babel message extractor for HTML `data-i18n` / `data-i18n-attr` attributes.

Referenced from babel_webui.cfg by dotted path
`iac_code.i18n.html_extractor.extract_html` (colon form breaks .cfg parsing).
"""

import re
from typing import Iterator

_I18N = re.compile(r'data-i18n="([^"]+)"')
_I18N_ATTR = re.compile(r'data-i18n-attr="([^"]+)"')


def extract_html(fileobj, keywords, comment_tags, options) -> Iterator[tuple[int, None, str, list]]:
    data = fileobj.read()
    if isinstance(data, bytes):
        data = data.decode("utf-8")
    for lineno, line in enumerate(data.splitlines(), 1):
        for match in _I18N.finditer(line):
            yield (lineno, None, match.group(1), [])
        for match in _I18N_ATTR.finditer(line):
            for part in match.group(1).split(";"):
                if ":" in part:
                    _attr, msgid = part.split(":", 1)
                    msgid = msgid.strip()
                    if msgid:
                        yield (lineno, None, msgid, [])
