"""Stable per-device maintenance identity shared by supervision backends."""

import uuid


def startup_identity(device_uuid):
    prefix = "praximesh:gpu:startup:v1:" + device_uuid
    return uuid.uuid5(uuid.NAMESPACE_URL, prefix + ":job"), uuid.uuid5(
        uuid.NAMESPACE_URL, prefix + ":reservation"
    )
