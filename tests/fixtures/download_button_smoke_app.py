"""Diagnostic fixture for Streamlit ZIP download delivery variants."""
import io
import zipfile

import streamlit as st


def make_payload() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "fixture/manifest.json",
            '{"metadata":{"game_id":"browser-download-smoke","status":"completed"}}',
        )
        archive.writestr(
            "fixture/PRIVATE_ARCHIVE_NOTICE.txt",
            "Synthetic test fixture only; contains no user data.\n",
        )
    return buffer.getvalue()


ZIP_PAYLOAD = make_payload()
st.title("Private archive ZIP browser smoke")
st.caption(f"Prepared cached ZIP bytes: {len(ZIP_PAYLOAD)}")
st.download_button(
    "Download ZIP bytes ignore",
    data=ZIP_PAYLOAD,
    file_name="bytes-ignore.zip",
    mime="application/zip",
    on_click="ignore",
    key="zip_bytes_ignore",
)
st.download_button(
    "Download ZIP callable ignore",
    data=lambda payload=ZIP_PAYLOAD: payload,
    file_name="callable-ignore.zip",
    mime="application/zip",
    on_click="ignore",
    key="zip_callable_ignore",
)
st.download_button(
    "Download ZIP bytes rerun",
    data=ZIP_PAYLOAD,
    file_name="bytes-rerun.zip",
    mime="application/zip",
    key="zip_bytes_rerun",
)
st.download_button(
    "Download ZIP callable rerun",
    data=lambda payload=ZIP_PAYLOAD: payload,
    file_name="callable-rerun.zip",
    mime="application/zip",
    key="zip_callable_rerun",
)
