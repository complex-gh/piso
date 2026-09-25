import hashlib
import time

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase

from core.crypto import canonical_auth_message, verify_ed25519
from core.gateway_auth import verify_bound_auth
from core.envelope import open_envelope, seal_payload, wrap_dek
from core.models import Account, AccountMembership, PairingRequest, Principal, PrincipalKey
from core.models.events import PairingStatus
from core.models.identity import KeyPurpose, MembershipRole, PrincipalKind
from nacl.signing import SigningKey


class PairingTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_create_poll_approve_and_reapprove(self):
        sign = SigningKey.generate()
        enc = "ab" * 32
        res = self.client.post(
            "/api/v1/pairing-requests",
            data={
                "kind": "gateway",
                "device_pubkey": sign.verify_key.encode().hex(),
                "encryption_pubkey": enc,
                "name": "Virginia",
            },
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 201, res.content)
        body = res.json()
        pid = body["id"]
        token = body["poll_token"]
        rec = PairingRequest.objects.get(pk=pid)
        self.assertEqual(rec.poll_token, hashlib.sha256(token.encode()).hexdigest())
        self.assertNotEqual(rec.poll_token, token)

        denied = self.client.get(f"/api/v1/pairing-requests/{pid}")
        self.assertEqual(denied.status_code, 401)
        query = self.client.get(f"/api/v1/pairing-requests/{pid}?token={token}")
        self.assertEqual(query.status_code, 401)

        status = self.client.get(
            f"/api/v1/pairing-requests/{pid}",
            HTTP_X_PISO_POLL_TOKEN=token,
        )
        self.assertEqual(status.json()["status"], "pending")

        user = User.objects.create_user("alice", password="secret")
        account = Account.objects.create(name="Alice")
        AccountMembership.objects.create(account=account, user=user, role=MembershipRole.OWNER)
        self.client.force_login(user)
        approve = self.client.post(f"/pair/{pid}/", {"action": "approve"})
        self.assertEqual(approve.status_code, 302)

        rec.refresh_from_db()
        self.assertEqual(rec.status, PairingStatus.APPROVED)
        self.assertEqual(rec.principal.kind, PrincipalKind.GATEWAY)
        self.assertTrue(
            PrincipalKey.objects.filter(
                principal=rec.principal, purpose=KeyPurpose.ENCRYPTION, public_key=enc
            ).exists()
        )
        first_id = rec.principal_id

        rec.status = PairingStatus.PENDING
        rec.save(update_fields=["status"])
        again = self.client.post(f"/pair/{pid}/", {"action": "approve"})
        self.assertEqual(again.status_code, 302)
        rec.refresh_from_db()
        self.assertEqual(rec.principal_id, first_id)
        self.assertEqual(Principal.objects.filter(nostr_pubkey=rec.device_pubkey).count(), 1)

    def test_sync_secrets_requires_gateway_auth(self):
        res = self.client.get("/api/v1/sync/secrets")
        self.assertEqual(res.status_code, 401)

    def test_create_pairing_requires_encryption_key(self):
        res = self.client.post(
            "/api/v1/pairing-requests",
            data={"kind": "gateway", "device_pubkey": "ab" * 32},
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 400)


class CryptoTests(TestCase):
    def test_canonical_auth_and_verify(self):
        sk = SigningKey.generate()
        pub = sk.verify_key.encode().hex()
        msg = canonical_auth_message("1", pub, "GET", "/p", "00", "ff" * 16)
        sig = sk.sign(msg).signature.hex()
        self.assertTrue(verify_ed25519(pub, msg, sig))
        self.assertFalse(verify_ed25519(pub, msg + b"x", sig))

    def test_nonce_reuse_rejected(self):
        cache.clear()
        sk = SigningKey.generate()
        pub = sk.verify_key.encode().hex()
        ts = str(int(time.time()))
        nonce = "ab" * 16
        path = "/api/v1/sync/secrets"
        body = b""
        msg = canonical_auth_message(
            ts, pub, "GET", path, hashlib.sha256(body).hexdigest(), nonce
        )
        sig = sk.sign(msg).signature.hex()
        self.assertTrue(verify_bound_auth(pub, ts, nonce, sig, "GET", path, body))
        self.assertFalse(verify_bound_auth(pub, ts, nonce, sig, "GET", path, body))

    def test_envelope_roundtrip(self):
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, NoEncryption, PublicFormat

        priv = X25519PrivateKey.generate()
        pub = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        priv_raw = priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
        dek, nonce, ct = seal_payload(b"hello-secret")
        wrap = wrap_dek(pub, dek)
        got = open_envelope(priv_raw, wrap, nonce, ct)
        self.assertEqual(got, b"hello-secret")
