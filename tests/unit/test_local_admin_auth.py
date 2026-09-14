"""本地管理员口令封装：加盐、参数被核对而非采纳、畸形封装一律拒绝。"""

import base64

from xiaowei_agent.interfaces.local_admin_auth import hash_password, verify_password


def test_hash_is_salted_so_two_hashes_differ() -> None:
    password = "correct-horse" + "-battery"
    assert hash_password(password) != hash_password(password)


def test_verify_accepts_the_original_and_rejects_others() -> None:
    password = "correct-horse" + "-battery"
    encoded = hash_password(password)
    assert verify_password(password, encoded) is True
    assert verify_password(password + "x", encoded) is False


def test_encoding_records_algorithm_and_parameters() -> None:
    encoded = hash_password("pw" + "-placeholder")
    assert encoded.startswith("scrypt$1$")
    assert "16384" in encoded  # n = 2**14


def test_malformed_encodings_are_rejected_without_raising() -> None:
    for bad in ["", "scrypt$1$", "bcrypt$x$y$z", "scrypt$9$16384$8$1$aa$bb"]:
        assert verify_password("pw", bad) is False


def test_encoded_parameters_are_checked_not_trusted() -> None:
    """封装里的 n/r/p 不得被当成计算输入，否则可被降成廉价运算或内存耗尽。"""
    password = "pw" + "-placeholder"
    encoded = hash_password(password)
    _, version, _, r, p, salt, dk = encoded.split("$")
    weakened = "$".join(["scrypt", version, "2", r, p, salt, dk])
    inflated = "$".join(["scrypt", version, "1048576", r, p, salt, dk])
    assert verify_password(password, weakened) is False
    assert verify_password(password, inflated) is False


def test_truncated_salt_or_hash_is_rejected() -> None:
    password = "pw" + "-placeholder"
    kind, version, n, r, p, _, dk = hash_password(password).split("$")
    short_salt = base64.b64encode(b"\x00" * 4).decode("ascii")
    assert verify_password(password, "$".join([kind, version, n, r, p, short_salt, dk])) is False
