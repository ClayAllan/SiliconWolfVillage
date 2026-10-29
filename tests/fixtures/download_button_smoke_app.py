"""Minimal browser fixture for Streamlit's deferred ZIP download path."""
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
    "Download ZIP fixture",
    data=lambda payload=ZIP_PAYLOAD: payload,
    file_name="private-archive-smoke.zip",
    mime="application/zip",
    on_click="ignore",
    key="zip_fixture",
)
