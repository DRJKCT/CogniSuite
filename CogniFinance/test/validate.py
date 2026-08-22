"""
Validates reference_reimpl.py against the real `spake2` library across
many random trials (random passwords, random entropy), not just one
fixed vector -- a single matching test vector could pass by coincidence
if there were a bug that happens to cancel out for specific inputs.
"""
import os
import sys
import binascii

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from spake2 import SPAKE2_A, SPAKE2_B
import reference_reimpl as ref


def run_trial(password: bytes) -> bool:
    entropy_a = os.urandom(64)
    entropy_b = os.urandom(64)

    real_a = SPAKE2_A(password, entropy_f=lambda n: entropy_a)
    real_b = SPAKE2_B(password, entropy_f=lambda n: entropy_b)
    real_msg_a = real_a.start()
    real_msg_b = real_b.start()
    real_key_a = real_a.finish(real_msg_b)
    real_key_b = real_b.finish(real_msg_a)
    assert real_key_a == real_key_b, "real library disagreed with itself -- shouldn't happen"

    side_a = ref.spake2_side("A", password, fixed_entropy_64=entropy_a)
    side_b = ref.spake2_side("B", password, fixed_entropy_64=entropy_b)

    if side_a["outbound_message"] != real_msg_a:
        print(f"  MISMATCH: outbound message A differs")
        return False
    if side_b["outbound_message"] != real_msg_b:
        print(f"  MISMATCH: outbound message B differs")
        return False

    my_key_a = ref.spake2_finish("A", password, side_a["x"], side_a["outbound_message"], side_b["outbound_message"])
    my_key_b = ref.spake2_finish("B", password, side_b["x"], side_b["outbound_message"], side_a["outbound_message"])

    if my_key_a != real_key_a:
        print(f"  MISMATCH: key A differs (real={real_key_a.hex()}, mine={my_key_a.hex()})")
        return False
    if my_key_b != real_key_b:
        print(f"  MISMATCH: key B differs (real={real_key_b.hex()}, mine={my_key_b.hex()})")
        return False
    return True


def main():
    passwords = [
        b"123456", b"000000", b"999999", b"", b"a", b"x" * 100,
        b"correct horse battery staple", os.urandom(6).hex().encode(),
    ]
    # plus a bunch of random 6-digit-style PINs, matching real usage
    for _ in range(20):
        passwords.append(f"{int.from_bytes(os.urandom(3), 'big') % 1_000_000:06d}".encode())

    passed = 0
    failed = 0
    for pw in passwords:
        for trial in range(3):  # multiple entropy draws per password
            ok = run_trial(pw)
            if ok:
                passed += 1
            else:
                failed += 1
                print(f"FAILED: password={pw!r} trial={trial}")

    print()
    print(f"{passed} passed, {failed} failed, out of {passed + failed} total trials")
    if failed == 0:
        print("ALL TRIALS MATCH THE REAL LIBRARY BIT-FOR-BIT.")
    else:
        print("THERE ARE MISMATCHES -- DO NOT PORT THIS TO KOTLIN YET.")
    return failed == 0


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
