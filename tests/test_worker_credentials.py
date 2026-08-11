import pytest
import inspect

from maxmcp.worker import credentials
from maxmcp.worker.credentials import CredentialError, load_worker_token


def test_load_worker_token_rejects_missing_or_short_secret() -> None:
    with pytest.raises(CredentialError):
        load_worker_token(reader=lambda _target: None)
    with pytest.raises(CredentialError):
        load_worker_token(reader=lambda _target: "short")


def test_load_worker_token_reads_named_windows_credential() -> None:
    targets = []
    token = load_worker_token(reader=lambda target: targets.append(target) or "a" * 40)
    assert token == "a" * 40
    assert targets == ["ARTOKE/MotionWorkerToken"]


def test_windows_credential_pointer_functions_declare_64_bit_safe_types() -> None:
    source = inspect.getsource(credentials._read_windows_credential)
    assert "CredFree.argtypes" in source
    assert "CredFree.restype" in source
