# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 lnfnt <lnfnt@proton.me>

"""S2-4: антибот/Qrator HTML-страница в URL-режиме не сохраняется как «официальный текст НПА»."""
from __future__ import annotations

import pytest

from sud_cli.npa_downloader import NpaDownloadError, download_npa


class _FakeTransport:
    def __init__(self, body: bytes, content_type: str = ""):
        self._body = body
        self._ct = content_type

    def __call__(self, method, url, headers, timeout):
        class _Resp:
            status = 200
            body = self._body
            headers = {"Content-Type": self._ct} if self._ct else {}
        return _Resp()


@pytest.mark.parametrize("ct,body", [
    ("text/html", b"<html><title>Qrator</title></html>"),
    ("", b"<!DOCTYPE html><html>blocked</html>"),
    ("text/html; charset=utf-8", "<html>доступ ограничен</html>".encode("utf-8")),
])
def test_antibot_html_refused_in_url_mode(tmp_path, ct, body):
    with pytest.raises(NpaDownloadError, match="S2-4|not a PDF or plain-text"):
        download_npa(url="https://vsrf.ru/doc/x.pdf", out_dir=tmp_path,
                     transport=_FakeTransport(body=body, content_type=ct))


def test_plain_text_url_still_accepted(tmp_path):
    res = download_npa(url="https://vsrf.ru/doc/x", out_dir=tmp_path,
                       transport=_FakeTransport(
                           body="Текст акта".encode("utf-8"),
                           content_type="text/plain; charset=utf-8"))
    assert res["ok"] is not False, res
