"""Task-owned live WSGI surface, driven manually and stopped by private IPC."""

from __future__ import annotations

import json
import re
import socket
import sys
from pathlib import Path
from socketserver import ThreadingMixIn
from threading import Thread, current_thread
from typing import TYPE_CHECKING
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

import pytest
from apps.consent.models import ConsentPurpose, ConsentText, NoticeTopic
from apps.consent.services import publish_notice, publish_text
from apps.ehr.models import Encounter
from apps.ehr.services import SOAP_FIELDS, publish_template
from apps.tenancy.db import tenant_context
from django.conf import settings
from django.contrib.staticfiles.handlers import StaticFilesHandler
from django.core.wsgi import get_wsgi_application
from django.db import connection, connections
from django.db.backends.signals import connection_created
from django.test import Client
from django.test.signals import language_changed, reset_template_engines

from consent._owned_surface_state import observe_surface
from consent.test_record_scope import seed_scope_world
from identity.permission_support import owner_context
from patient_service_support import runtime_role
from renewal.test_retention import staff_client

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from wsgiref.types import StartResponse, WSGIEnvironment

    from django.db.backends.base.base import BaseDatabaseWrapper

    from rbac_fixtures import RbacGraph

PRIVATE = Path("/home/samarone/.t20s5ec")
UUID_TOKEN = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")


class QuietHandler(WSGIRequestHandler):
    def log_message(self, *args: object, **kwargs: object) -> None:
        """Never print a clinical selector or a fixture credential."""


class ThreadedWSGIServer(ThreadingMixIn, WSGIServer):
    """Join every owned request worker when the fixture closes its server."""


class RuntimeWSGI:
    """Own the role-bound WSGI callbacks and paired server/control lifetime."""

    def __init__(self, observe: Callable[[str], dict[str, object]]) -> None:
        self.observe = observe
        self.application = StaticFilesHandler(get_wsgi_application())
        self.trace: list[dict[str, object]] = []
        self.request_threads: set[Thread] = set()
        self.server = make_server(
            "127.0.0.1",
            0,
            self.serve,
            handler_class=QuietHandler,
            server_class=ThreadedWSGIServer,
        )
        self.thread = Thread(
            target=self.server.serve_forever, name="cpi-t20-r4-http", daemon=False
        )

    def serve(
        self, environ: WSGIEnvironment, start_response: StartResponse
    ) -> Iterable[bytes]:
        if str(environ["PATH_INFO"]).startswith("/static/"):
            response = self.application(environ, start_response)
            try:
                yield from response
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
            return
        thread = current_thread()
        self.request_threads.add(thread)
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_user")
                role = cursor.fetchone()[0]
            assert role == "clinic_app", role
            self.trace.append(
                {
                    "method": environ["REQUEST_METHOD"],
                    "path": UUID_TOKEN.sub("[FIXTURE-ID]", str(environ["PATH_INFO"])),
                    "role": role,
                }
            )
            response = self.application(environ, start_response)
            try:
                yield from response
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
        finally:
            connections.close_all()
            self.request_threads.discard(thread)

    def runtime_connection(
        self, sender: object, connection: BaseDatabaseWrapper, **kwargs: object
    ) -> None:
        if current_thread() in self.request_threads:
            with connection.cursor() as cursor:
                cursor.execute("SET ROLE clinic_app")

    def run(self, metadata: dict[str, object]) -> None:
        control_path = PRIVATE / "r1-http-control.sock"
        assert not control_path.exists()
        control = socket.socket(socket.AF_UNIX)
        control.bind(str(control_path))
        control.listen(1)
        control.settimeout(1700)
        connection_created.connect(self.runtime_connection, weak=False)
        state = {
            **metadata,
            "base_url": f"http://127.0.0.1:{self.server.server_port}",
            "control": str(control_path),
        }
        try:
            (PRIVATE / "r1-http-metadata.json").write_text(
                json.dumps(state, indent=2) + "\n", encoding="utf-8"
            )
            self.thread.start()
            sys.stdout.write(
                "SURFACE_READY "
                + json.dumps(
                    {
                        "base_url": state["base_url"],
                        "metadata": str(PRIVATE / "r1-http-metadata.json"),
                        "control": str(control_path),
                    }
                )
                + "\n"
            )
            sys.stdout.flush()
            while True:
                client, _address = control.accept()
                with client:
                    command = client.recv(128).decode("ascii").strip()
                    if command == "STOP":
                        client.sendall(b"STOPPING\n")
                        break
                    if command == "RELOAD":
                        reset_template_engines(setting="TEMPLATES")
                        language_changed(setting="LOCALE_PATHS")
                        client.sendall(b"RELOADED\n")
                    else:
                        client.sendall(
                            (json.dumps(self.observe(command)) + "\n").encode()
                        )
        finally:
            if self.thread.is_alive():
                self.server.shutdown()
                self.thread.join(timeout=10)
            self.server.server_close()
            control.close()
            control_path.unlink()
            connection_created.disconnect(self.runtime_connection)
            connections.close_all()
            (PRIVATE / "r1-http-trace.json").write_text(
                json.dumps(self.trace, indent=2) + "\n", encoding="utf-8"
            )
            sys.stdout.write(
                "SURFACE_CLEANED "
                + json.dumps({"thread_alive": self.thread.is_alive()})
                + "\n"
            )
            sys.stdout.flush()
        assert not self.thread.is_alive()
        assert self.trace
        assert all(row["role"] == "clinic_app" for row in self.trace)


@pytest.mark.django_db(transaction=True)
def test_owned_consent_surface(rbac_graph: RbacGraph) -> None:
    """Keep real fixture authority alive for explicitly controlled manual QA."""
    world = seed_scope_world(rbac_graph)
    with owner_context(world.organization):
        physician_id = Encounter.objects.get(pk=world.encounter_a).physician_id
    manager_id = rbac_graph.clinic_admin
    with runtime_role(), tenant_context(manager_id, world.organization):
        publish_template(
            clinic_id=world.clinic_a,
            key="consent-manual-source",
            title="Modelo sintético para anotação manual",
            prompts=dict.fromkeys(SOAP_FIELDS, ""),
        )
        for purpose in ConsentPurpose.values:
            publish_text(
                clinic_id=world.clinic_a,
                purpose=purpose,
                text=f"Texto sintético independente para {purpose}.",
                language="pt-BR",
            )
        for topic in NoticeTopic.values:
            publish_notice(
                clinic_id=world.clinic_a,
                topic=topic,
                text="Aviso informativo sintético. Não solicita autorização.",
                language="pt-BR",
            )
    patient = Client()
    patient_session = patient.session
    patient_session["patient_session_id"] = str(world.session_a)
    patient_session.save()
    with staff_client(physician_id) as staff:
        staff_cookie = staff.cookies[settings.SESSION_COOKIE_NAME].value
    with staff_client(manager_id) as manager:
        manager_cookie = manager.cookies[settings.SESSION_COOKIE_NAME].value
    with owner_context(world.organization):
        texts = [
            {"id": str(row.pk), "purpose": row.purpose, "version": row.version}
            for row in ConsentText.objects.filter(clinic_id=world.clinic_a).order_by(
                "purpose", "-version"
            )
        ]
    RuntimeWSGI(lambda command: observe_surface(world, command)).run(
        {
            "clinic_id": str(world.clinic_a),
            "encounter_id": str(world.encounter_a),
            "enrollment_id": str(world.enrollment_a),
            "physician_id": str(physician_id),
            "manager_id": str(manager_id),
            "patient_cookie": patient.cookies[settings.SESSION_COOKIE_NAME].value,
            "staff_cookie": staff_cookie,
            "manager_cookie": manager_cookie,
            "cookie_name": settings.SESSION_COOKIE_NAME,
            "texts": texts,
        }
    )
