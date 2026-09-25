import hashlib
import os
import time

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase

import base64
import uuid

from core.crypto import canonical_auth_message, verify_ed25519
from core.gateway_auth import verify_bound_auth
from core.models import Account, AccountMembership, Gateway, PairingRequest, Principal, PrincipalKey
from core.models.access import Resource
from core.models.events import PairingStatus
from core.models.identity import KeyPurpose, MembershipRole, PrincipalKind, PrincipalStatus
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


class GrantTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("alice", password="secret", is_staff=True)
        self.account = Account.objects.create(name="Alice")
        AccountMembership.objects.create(
            account=self.account, user=self.user, role=MembershipRole.OWNER
        )
        self.gw = Principal.objects.create(
            account=self.account,
            kind=PrincipalKind.GATEWAY,
            nostr_pubkey="ab" * 32,
            name="Virginia",
            status=PrincipalStatus.ACTIVE,
        )
        PrincipalKey.objects.create(
            principal=self.gw, purpose=KeyPurpose.ENCRYPTION, public_key="cd" * 32
        )
        Gateway.objects.create(principal=self.gw, machine_name="Virginia")
        self.client.force_login(self.user)

    def test_anonymous_pair_redirects_to_login(self):
        self.client.logout()
        pid = uuid.uuid4()
        res = self.client.get(f"/pair/{pid}/")
        self.assertEqual(res.status_code, 302)
        self.assertIn("/login/", res["Location"])
        self.assertIn("next=", res["Location"])

    def test_reject_plaintext_secret(self):
        res = self.client.post(
            "/api/v1/secrets",
            data={"name": "x", "value": "secret", "wraps": []},
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 400)

    def test_create_secret_stores_ciphertext_only(self):
        wrap = base64.b64encode(b"\x01" + b"\x00" * 92).decode()
        nonce = base64.b64encode(os.urandom(12)).decode()
        ct = base64.b64encode(os.urandom(32)).decode()
        res = self.client.post(
            "/api/v1/secrets",
            data={
                "name": "Anthropic",
                "algorithm": "chacha20poly1305-v1",
                "nonce": nonce,
                "ciphertext": ct,
                "content_hash": "00",
                "metadata": {"placeholder": "piso_x"},
                "wraps": [
                    {"principal_id": str(self.gw.id), "wrapped_data_key": wrap}
                ],
            },
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 201, res.content)
        listed = self.client.get("/api/v1/secrets")
        self.assertEqual(listed.status_code, 200)
        body = listed.json()
        self.assertEqual(len(body["secrets"]), 1)
        self.assertNotIn("ciphertext", body["secrets"][0])
        self.assertNotIn("value", body["secrets"][0])
        self.assertTrue(Resource.objects.filter(name="Anthropic").exists())

    def test_list_gateways(self):
        res = self.client.get("/api/v1/gateways")
        self.assertEqual(res.status_code, 200)
        rows = res.json()["gateways"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["encryption_pubkey"], "cd" * 32)
