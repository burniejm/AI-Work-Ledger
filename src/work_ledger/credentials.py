"""Credential store abstraction (DESIGN §14, Global Constraints).

Secrets never live in SQLite, config, or logs. Config and SQLite hold only a
*reference* -- `keychain://work-ledger/<connection>` or `env://VAR` -- and
this module is the only place that turns a reference into the actual secret.

Platform support (DESIGN gap G7): macOS uses the `security` CLI via
`KeychainStore`. Windows and Linux are stubbed (`WindowsCredentialStore`,
`LinuxSecretServiceStore`) -- both report `available() == False` and raise
`Unsupported` rather than silently no-op, so a caller can tell "no backing
store" apart from "credential not set". `env://` references (`EnvRefStore`)
work everywhere and are read-only -- there's nothing to "set" or "delete" in
the calling process's own environment.

`resolve()` is the convenience entry point most callers want: it never
raises, returning `None` for anything that can't be resolved (bad reference,
unsupported platform, missing item) so a missing credential degrades a
connection rather than crashing a report.

This module aligns with `uninstall.py`'s existing `security` plumbing
(`_delete_keychain_entry`): same `service`/`account` split of a
`keychain://<service>/<account>` reference, same argv-list-never-shell
subprocess discipline, so the two call sites agree on what a keychain
reference means.
"""
import os
import subprocess
import sys
from abc import ABC, abstractmethod

from work_ledger import redact

_SECURITY_BIN = "/usr/bin/security"


class Unsupported(Exception):
    """A credential-store operation that this platform/store cannot perform."""


class InvalidReference(Exception):
    """`reference` is not a well-formed `keychain://...` or `env://...` string."""


def parse_reference(reference: str) -> "tuple[str, str, str]":
    """Parse a credential reference into `(scheme, service, account_or_var)`.

    `keychain://work-ledger/acme-jira` -> `("keychain", "work-ledger", "acme-jira")`
    `env://JIRA_TOKEN` -> `("env", "", "JIRA_TOKEN")`
    Anything else raises `InvalidReference`.
    """
    if not isinstance(reference, str):
        raise InvalidReference(reference)
    if reference.startswith("keychain://"):
        rest = reference[len("keychain://"):]
        if "/" not in rest:
            raise InvalidReference(reference)
        service, account = rest.split("/", 1)
        if not service or not account:
            raise InvalidReference(reference)
        return "keychain", service, account
    if reference.startswith("env://"):
        var = reference[len("env://"):]
        if not var:
            raise InvalidReference(reference)
        return "env", "", var
    raise InvalidReference(reference)


def _safe_diagnostic(text: "str | None") -> str:
    """Scrub subprocess stderr/stdout before it could ever end up in a log
    line or exception message (Global Constraints: secrets never in logs or
    diagnostic output). `security` does not normally echo secret material
    back on stderr, but this is defense in depth against a future change."""
    text = (text or "").strip()
    if not text:
        return ""
    return "[REDACTED]" if redact.looks_like_secret(text) else text


class CredentialStore(ABC):
    """Common interface every credential backend implements."""

    scheme: str

    @abstractmethod
    def available(self) -> bool:
        """Whether this store can actually be used on this system."""

    @abstractmethod
    def get(self, reference: str) -> "str | None":
        """Fetch the secret for `reference`, or `None` if it isn't set."""

    @abstractmethod
    def set(self, reference: str, secret: str) -> None:
        """Store `secret` under `reference`."""

    @abstractmethod
    def delete(self, reference: str) -> None:
        """Remove the secret for `reference`, if any."""


class KeychainStore(CredentialStore):
    """macOS Keychain, via the `security` CLI (argv lists, subprocess shell never enabled)."""

    scheme = "keychain"

    def available(self) -> bool:
        return sys.platform == "darwin" and os.path.exists(_SECURITY_BIN)

    def get(self, reference: str) -> "str | None":
        _, service, account = parse_reference(reference)
        try:
            proc = subprocess.run(
                [_SECURITY_BIN, "find-generic-password", "-a", account, "-s", service, "-w"],
                capture_output=True, text=True, timeout=5,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None
        if proc.returncode != 0:
            return None
        return proc.stdout.rstrip("\n")

    def set(self, reference: str, secret: str) -> None:
        _, service, account = parse_reference(reference)
        argv = [_SECURITY_BIN, "add-generic-password", "-U", "-a", account, "-s", service, "-w"]
        # Spec §14.1 argv-exposure mitigation: only pass the secret as an
        # argv token when stdin isn't a tty (e.g. scripted/non-interactive
        # use). On a tty, `-w` is left bare so `security` prompts instead,
        # keeping the secret out of argv (and thus out of `ps`/shell history).
        if not sys.stdin.isatty():
            argv.append(secret)
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=5)
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise Unsupported(f"could not run security add-generic-password: {exc}") from None
        if proc.returncode != 0:
            raise Unsupported(f"security add-generic-password failed: {_safe_diagnostic(proc.stderr or proc.stdout)}")

    def delete(self, reference: str) -> None:
        _, service, account = parse_reference(reference)
        try:
            subprocess.run(
                [_SECURITY_BIN, "delete-generic-password", "-a", account, "-s", service],
                capture_output=True, text=True, timeout=5,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise Unsupported(f"could not run security delete-generic-password: {exc}") from None


class EnvRefStore(CredentialStore):
    """`env://VAR` -- reads the calling process's own environment. Read-only:
    there's no sense in which this module should mutate the environment."""

    scheme = "env"

    def available(self) -> bool:
        return True

    def get(self, reference: str) -> "str | None":
        _, _, var = parse_reference(reference)
        return os.environ.get(var)

    def set(self, reference: str, secret: str) -> None:
        raise Unsupported("env:// references are read-only; set the environment variable instead")

    def delete(self, reference: str) -> None:
        raise Unsupported("env:// references are read-only; unset the environment variable instead")


class UnsupportedStore(CredentialStore):
    """Base for platform stubs (DESIGN gap G7): never available, every
    operation raises `Unsupported` rather than silently no-op-ing, so callers
    can tell "no backing store on this platform" apart from "not set"."""

    scheme = "keychain"

    def available(self) -> bool:
        return False

    def _unsupported(self) -> Unsupported:
        return Unsupported(f"{type(self).__name__} is not implemented; keychain:// credentials "
                            f"are not supported on this platform")

    def get(self, reference: str) -> "str | None":
        raise self._unsupported()

    def set(self, reference: str, secret: str) -> None:
        raise self._unsupported()

    def delete(self, reference: str) -> None:
        raise self._unsupported()


class WindowsCredentialStore(UnsupportedStore):
    """Stub -- Windows Credential Manager is not implemented in v1."""


class LinuxSecretServiceStore(UnsupportedStore):
    """Stub -- the Secret Service (libsecret) is not implemented in v1."""


def get_store(reference: str) -> CredentialStore:
    """Dispatch a reference to the right `CredentialStore` for this scheme
    and platform. Raises `InvalidReference` for an unrecognized scheme."""
    scheme, _, _ = parse_reference(reference)
    if scheme == "env":
        return EnvRefStore()
    if scheme == "keychain":
        if sys.platform == "darwin":
            return KeychainStore()
        if sys.platform.startswith("win"):
            return WindowsCredentialStore()
        return LinuxSecretServiceStore()
    raise InvalidReference(reference)


def resolve(reference: str) -> "str | None":
    """Look up the secret for `reference`. Never raises -- a bad reference,
    an unsupported platform, or a missing item all resolve to `None` so a
    missing credential degrades a connection rather than crashing a report.

    Every value this actually returns is also registered with
    `redact.register_known_secret` -- this is the one place in the codebase
    a reference turns into the real secret, so it is also the one place
    that can teach the redaction layer an exact value to catch verbatim
    wherever it later appears, regardless of shape (Task 38 review: a
    shape-based pattern broad enough to catch an arbitrary human-chosen
    password is indistinguishable from an ordinary identifier; an exact
    match against a value this process is known to have resolved as a
    credential has no such false-positive mode).
    """
    try:
        store = get_store(reference)
    except InvalidReference:
        return None
    if not store.available():
        return None
    try:
        value = store.get(reference)
    except Unsupported:
        return None
    redact.register_known_secret(value)
    return value
