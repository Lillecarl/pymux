"""
`org.freedesktop.portal.Settings`, with the one setting pymux knows.

GTK 4, libadwaita, Qt 6, Firefox and Chromium read the colour scheme
from here: `org.freedesktop.appearance color-scheme`, a `u` that is 0
for no preference, 1 for dark and 2 for light. A key nobody serves
answers `org.freedesktop.portal.Error.NotFound`, which a program reads
as "the default", the way xdg-desktop-portal answers it.

dbus-fast reads each signature by evaluating the annotation against
this module, so every name an annotation uses is imported at runtime.
Lillecarl/pymux#812.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Callable
from typing import Annotated

from dbus_fast import DBusError, PropertyAccess, Variant
from dbus_fast.annotations import DBusSignature, DBusStr, DBusUInt32, DBusVariant
from dbus_fast.service import ServiceInterface, dbus_method, dbus_property, dbus_signal

__all__ = ["APPEARANCE", "BUS_NAME", "COLOR_SCHEME", "INTERFACE", "OBJECT_PATH", "Settings"]

BUS_NAME = "org.freedesktop.portal.Desktop"
OBJECT_PATH = "/org/freedesktop/portal/desktop"
INTERFACE = "org.freedesktop.portal.Settings"
APPEARANCE = "org.freedesktop.appearance"
COLOR_SCHEME = "color-scheme"

#: 2 brought `ReadOne`, and `Read` stays for the programs that call it.
VERSION = 2

NOT_FOUND = "org.freedesktop.portal.Error.NotFound"

Namespaces = Annotated[list[str], DBusSignature("as")]
AllSettings = Annotated[dict[str, dict[str, Variant]], DBusSignature("a{sa{sv}}")]


class Settings(ServiceInterface):
    "The interface, answering from `scheme`, which the portal reads afresh on each call."

    def __init__(self, scheme: Callable[[], int]) -> None:
        super().__init__(INTERFACE)
        self._scheme = scheme

    def _settings(self) -> dict[str, dict[str, Variant]]:
        return {APPEARANCE: {COLOR_SCHEME: Variant("u", self._scheme())}}

    def _one(self, namespace: str, key: str) -> Variant:
        try:
            return self._settings()[namespace][key]
        except KeyError:
            raise DBusError(NOT_FOUND, "Requested setting not found") from None

    @dbus_method()
    def ReadAll(self, namespaces: Namespaces) -> AllSettings:
        # A pattern may end in `*`; none, or one empty, asks for all.
        patterns = [one for one in namespaces if one] or ["*"]
        return {
            namespace: values
            for namespace, values in self._settings().items()
            if any(fnmatch.fnmatchcase(namespace, pattern) for pattern in patterns)
        }

    @dbus_method()
    def Read(self, namespace: DBusStr, key: DBusStr) -> DBusVariant:
        # Version 1's answer is the value inside a second variant, and
        # the programs that still call it unwrap two.
        return Variant("v", self._one(namespace, key))

    @dbus_method()
    def ReadOne(self, namespace: DBusStr, key: DBusStr) -> DBusVariant:
        return self._one(namespace, key)

    @dbus_signal()
    def SettingChanged(
        self, namespace: str, key: str, value: Variant
    ) -> Annotated[tuple[str, str, Variant], DBusSignature("ssv")]:
        return namespace, key, value

    @dbus_property(access=PropertyAccess.READ)
    def version(self) -> DBusUInt32:
        return VERSION

    def scheme_changed(self) -> None:
        "Tell every program on the bus, which switches live."
        self.SettingChanged(APPEARANCE, COLOR_SCHEME, Variant("u", self._scheme()))
