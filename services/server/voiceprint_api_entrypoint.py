"""Start voiceprint-api with a ModelScope chunked-response workaround.

ModelScope's downloader validates every response against Content-Length. The
ModelScope API may respond with chunked encoding for compressed requests, which
leaves that header absent and makes startup fail. Request identity encoding so
the server returns Content-Length while preserving ModelScope's normal cache
and integrity checks.
"""

import modelscope.hub.snapshot_download as snapshot_download

_original_http_get_file = snapshot_download.http_get_file


def _http_get_file_with_identity_encoding(
    url, local_dir, file_name, cookies, headers=None
):
    request_headers = dict(headers or {})
    request_headers["Accept-Encoding"] = "identity"
    return _original_http_get_file(
        url,
        local_dir,
        file_name,
        cookies,
        headers=request_headers,
    )


snapshot_download.http_get_file = _http_get_file_with_identity_encoding


def _run_server():
    from start_server import main

    main()


if __name__ == "__main__":
    _run_server()
