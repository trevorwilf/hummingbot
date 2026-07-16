"""Invariants for the deployment stack files (docker_files/).

CDX-002: the api service used to bind-mount a host-patched copy of
``services/docker_service.py`` over the image's own file. The patched copy was
generated once, on first boot, and then retained forever -- so rebuilt api code
(including the copy-forward hook wiring at docker_service.py:15/:309) never ran.
These tests lock the cutover: no mount, no patch machinery, provenance emitted,
and the env vars the patch used to supply are pinned explicitly.

Pure file-content tests. Nothing here runs, builds, or inspects docker.
"""

import datetime
import http.server
import json
import re
import shutil
import ssl
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlsplit

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKER_FILES = REPO_ROOT / "docker_files"
VPN_STACK = DOCKER_FILES / "hummingbot stack - vpn"
NO_VPN_STACK = DOCKER_FILES / "hummingbot stack - no vpn"
STACKS = (VPN_STACK, NO_VPN_STACK)

# The image path the removed bind-mount used to shadow.
SHADOWED_TARGET = "/hummingbot-api/services/docker_service.py"

API_INIT_SERVICE = "hummingbot-api-init"
API_SERVICE = "hummingbot-api"
GATEWAY_SERVICE = "gateway"


def _resolve_bash():
    """Return an absolute path to a working bash, or None.

    Bare "bash" is not usable via subprocess on Windows: CreateProcess searches
    System32 (where the WSL relay stub lives) before PATH, so the stub wins and
    fails with `execvpe(/bin/bash)`. Resolve to an absolute path and probe it.
    """
    candidates = [shutil.which("bash"), r"C:\Program Files\Git\bin\bash.exe", "/bin/bash"]
    for candidate in candidates:
        if not candidate or not Path(candidate).exists():
            continue
        try:
            probe = subprocess.run(
                [candidate, "-c", "echo hbok"], capture_output=True, text=True, timeout=60
            )
        except OSError:
            continue
        if probe.returncode == 0 and "hbok" in probe.stdout:
            return candidate
    return None


BASH = _resolve_bash()


def read_text(path):
    return path.read_text(encoding="utf-8")


def load_stack(path):
    return yaml.safe_load(read_text(path))


def service_of(stack, name):
    return load_stack(stack)["services"][name]


def command_text(service):
    """docker compose `command:` is either a string or a list of strings."""
    command = service.get("command", "")
    if isinstance(command, list):
        return "\n".join(str(part) for part in command)
    return str(command)


def volume_entries(service):
    """Yield every volume mapping as a string, tolerating the long (dict) form."""
    for entry in service.get("volumes", []) or []:
        if isinstance(entry, dict):
            yield f"{entry.get('source', '')}:{entry.get('target', '')}"
        else:
            yield str(entry)


def env_map(service):
    """Parse the `environment:` list form into a dict."""
    environment = service.get("environment", []) or []
    if isinstance(environment, dict):
        return {str(k): str(v) for k, v in environment.items()}
    parsed = {}
    for item in environment:
        key, _, value = str(item).partition("=")
        # strip trailing inline `# comment` -- compose treats it as part of the
        # value, but these tests only assert on the meaningful prefix.
        parsed[key.strip()] = value.split("#")[0].strip()
    return parsed


class TestStackFilesParse(unittest.TestCase):
    def test_both_stacks_are_valid_yaml_with_services(self):
        for stack in STACKS:
            with self.subTest(stack=stack.name):
                parsed = load_stack(stack)
                self.assertIsInstance(parsed, dict)
                self.assertIn(API_INIT_SERVICE, parsed["services"])
                self.assertIn(API_SERVICE, parsed["services"])


class TestPatchBindMountRemoved(unittest.TestCase):
    """CDX-002 core: the source bind-mount must be gone from both stacks."""

    def test_no_service_mounts_over_docker_service_py(self):
        # Semantic check: walks every service's volumes, so re-adding the mount
        # to ANY service (not just hummingbot-api) fails this test.
        for stack in STACKS:
            services = load_stack(stack)["services"]
            for name, service in services.items():
                for entry in volume_entries(service):
                    with self.subTest(stack=stack.name, service=name):
                        self.assertNotIn(
                            SHADOWED_TARGET,
                            entry,
                            f"{stack.name}: service '{name}' mounts over the image's "
                            f"docker_service.py ({entry!r}); CDX-002 forbids it.",
                        )

    def test_no_patches_docker_service_path_in_text(self):
        # Text-level backstop: catches the mount re-added in a form the YAML
        # walk above would not model (e.g. a commented-out line being restored).
        for stack in STACKS:
            with self.subTest(stack=stack.name):
                self.assertNotIn("patches/docker_service.py", read_text(stack))

    def test_patch_script_machinery_absent(self):
        for stack in STACKS:
            with self.subTest(stack=stack.name):
                text = read_text(stack)
                self.assertNotIn("PATCH_SCRIPT", text)
                self.assertNotIn("PATCH_DIR", text)
                self.assertNotIn("_get_bot_network_mode", text)
                self.assertNotIn("UPSTREAM_REF", text)

    def test_upstream_changed_warning_banner_absent(self):
        # The banner was unreachable anyway (the checksum was written before the
        # retain decision), but it must not survive the cutover.
        for stack in STACKS:
            with self.subTest(stack=stack.name):
                self.assertNotIn("UPSTREAM docker_service.py HAS CHANGED", read_text(stack))


class TestProvenanceEmitted(unittest.TestCase):
    """Boot provenance makes runtime-source-vs-image verification a log read.

    Static locks on the EXACT emission forms. Asserting that the words appear
    somewhere is not enough: pointing DS_FILE at the wrong file, or grepping for
    a symbol that cannot exist, leaves every word of the output in place while
    the emitted value becomes a lie. Each assertion below names the relationship
    (which file is hashed, which symbol is counted), not just the vocabulary.
    """

    def test_api_init_emits_provenance_for_docker_service(self):
        for stack in STACKS:
            with self.subTest(stack=stack.name):
                command = command_text(service_of(stack, API_INIT_SERVICE))
                self.assertIn("[provenance]", command)
                self.assertRegex(
                    command,
                    re.compile(r'^\s*DS_FILE="\$\$API_ROOT/services/docker_service\.py"\s*$', re.M),
                    "the hashed file must BE services/docker_service.py",
                )
                self.assertRegex(
                    command,
                    re.compile(
                        r'^\s*echo "\[provenance\] services/docker_service\.py '
                        r'\$\$\(provenance_hash "\$\$DS_FILE"\)',
                        re.M,
                    ),
                    "the docker_service.py provenance line must hash DS_FILE itself",
                )
                self.assertIn("sha256sum", command)
                self.assertIn("md5sum", command)

    def test_provenance_reports_seed_resume_state_counts(self):
        # seed_resume_state is DEFINED in services/resume_service.py:1462 and
        # imported/called by docker_service.py:15/:309 -- the wiring the retained
        # patch used to shadow. Provenance attests to both files.
        for stack in STACKS:
            with self.subTest(stack=stack.name):
                command = command_text(service_of(stack, API_INIT_SERVICE))
                self.assertRegex(
                    command,
                    re.compile(r'^\s*RS_FILE="\$\$API_ROOT/services/resume_service\.py"\s*$', re.M),
                    "the definition count must be taken over services/resume_service.py",
                )
                # The closing quote is the anchor: a widened or fabricated grep
                # pattern (e.g. 'def seed_resume_state_DOES_NOT_EXIST') no longer
                # matches these, so a count that can only ever be 0 fails here.
                self.assertIn(
                    """seed_resume_state_refs=$$(grep -c 'seed_resume_state' "$$DS_FILE" || true)""",
                    command,
                )
                self.assertIn(
                    """seed_resume_state_defs=$$(grep -c 'def seed_resume_state' "$$RS_FILE" || true)""",
                    command,
                )


class TestApiInitUnrelatedDutiesPreserved(unittest.TestCase):
    """Removal had to be surgical: api-init's seeding duties must survive.

    Counting occurrences of `version_aware_seed` is not enough -- the name
    survives inside a comment, a `:` no-op or a quoted string while the seed
    never runs. Each duty is locked to its anchored, executable command form.
    """

    # (source var, destination var, fingerprint tag) for each retained duty.
    SEED_DUTIES = (
        ("CTRL_SRC", "CTRL_DST", "api-ctrl"),
        ("SCRIPTS_SRC", "SCRIPTS_DST", "api-scripts"),
        ("CONF_SRC", "CONF_DST", "api-conf"),
    )

    def test_version_aware_seeds_still_present(self):
        for stack in STACKS:
            with self.subTest(stack=stack.name):
                command = command_text(service_of(stack, API_INIT_SERVICE))
                self.assertIn("seed_helpers.sh", command)
                self.assertIn("fingerprint_dir", command)
                for src, dst, tag in self.SEED_DUTIES:
                    # Anchored to start-of-line: the call must be the command the
                    # line executes, not text embedded in something inert.
                    pattern = re.compile(
                        rf'^\s*version_aware_seed "\$\${src}" "\$\${dst}" '
                        rf'"{tag}-\$\$API_VERSION"\s*$',
                        re.M,
                    )
                    self.assertRegex(
                        command,
                        pattern,
                        f"{stack.name}: api-init must still invoke version_aware_seed for "
                        f"{src} -> {dst}; removal of the patch machinery had to be surgical.",
                    )


class TestPatchSuppliedEnvPinned(unittest.TestCase):
    """Equivalence gate: behaviour the patch supplied must now come from the stack.

    The removed patch hard-coded its own fallbacks inside the injected helpers.
    The image's source (services/docker_service.py:46-72) reads the same values
    from the environment but with DIFFERENT defaults, so any value the stack does
    not pin explicitly would silently change on cutover.
    """

    def test_bot_network_mode_pinned_in_both_stacks(self):
        # Patch default was "none"; image source default is "host" (a VPN leak in
        # the vpn stack). Both stacks must set the var so the default is unreachable.
        # Exact equality, not containment: "none-typo" contains "none" but is not
        # the patch's fail-closed fallback, and docker_service.py passes whatever
        # it resolves straight to containers.run(network_mode=...).
        expected = {
            VPN_STACK: "${DOCKER_BOT_NETWORK_MODE:-none}",
            NO_VPN_STACK: "${DOCKER_BOT_NETWORK_MODE:-hbnet-us}",
        }
        for stack in STACKS:
            with self.subTest(stack=stack.name):
                value = env_map(service_of(stack, API_SERVICE)).get("DOCKER_BOT_NETWORK_MODE")
                self.assertEqual(
                    expected[stack],
                    value,
                    f"{stack.name}: DOCKER_BOT_NETWORK_MODE must pin the exact fallback the "
                    f"removed patch supplied (a host override stays possible).",
                )

    def test_compose_service_prefix_pinned_in_both_stacks(self):
        # The patch defaulted this to "hummingbot-bot" (vpn) / "hummingbot-us-bot"
        # (no-vpn); the image source defaults to "hummingbot-bot" for both. Only
        # the vpn stack pinned it, so the no-vpn com.docker.compose.service label
        # would have silently changed from hummingbot-us-bot-* to hummingbot-bot-*.
        expected = {VPN_STACK: "hummingbot-bot", NO_VPN_STACK: "hummingbot-us-bot"}
        for stack in STACKS:
            with self.subTest(stack=stack.name):
                value = env_map(service_of(stack, API_SERVICE)).get("COMPOSE_SERVICE_PREFIX")
                self.assertEqual(
                    expected[stack],
                    value,
                    f"{stack.name}: COMPOSE_SERVICE_PREFIX must be pinned to preserve "
                    f"the compose labels the removed patch produced.",
                )

    def test_compose_project_name_pinned_in_both_stacks(self):
        # _get_compose_labels adds the com.docker.compose.* labels only when this
        # resolves non-empty. A raw `${SOMETHING_UNSET}` is truthy as YAML text but
        # compose resolves it to "", silently dropping every compose label, so the
        # literal project identity is asserted rather than mere non-emptiness.
        expected = {VPN_STACK: "hummingbot_stack_1", NO_VPN_STACK: "hummingbot-us"}
        for stack in STACKS:
            with self.subTest(stack=stack.name):
                value = env_map(service_of(stack, API_SERVICE)).get("COMPOSE_PROJECT_NAME")
                self.assertEqual(
                    expected[stack],
                    value,
                    f"{stack.name}: COMPOSE_PROJECT_NAME must be the literal project name so "
                    f"spawned bots keep their pre-cutover compose labels.",
                )


@unittest.skipIf(BASH is None, "no working bash available to syntax-check api-init")
class TestApiInitShellIsValid(unittest.TestCase):
    """The api-init command is shell; YAML validity does not imply shell validity."""

    def test_api_init_command_passes_bash_syntax_check(self):
        for stack in STACKS:
            with self.subTest(stack=stack.name):
                # compose escapes a literal '$' as '$$'; undo it to recover the
                # script the container actually runs.
                script = command_text(service_of(stack, API_INIT_SERVICE)).replace("$$", "$")
                with tempfile.NamedTemporaryFile(
                    "w", suffix=".sh", delete=False, encoding="utf-8", newline="\n"
                ) as handle:
                    handle.write(script)
                    script_path = handle.name
                try:
                    result = subprocess.run(
                        [BASH, "-n", Path(script_path).as_posix()], capture_output=True, text=True
                    )
                    self.assertEqual(
                        0, result.returncode, f"{stack.name} api-init shell error: {result.stderr}"
                    )
                finally:
                    Path(script_path).unlink(missing_ok=True)


@unittest.skipIf(BASH is None, "no working bash available to execute provenance block")
class TestProvenanceBlockBehaviour(unittest.TestCase):
    """Execute the real provenance shell against a fake image tree.

    Only the candidate-root search line is rewritten (the real roots are absolute
    container paths that cannot exist on a test host); the hashing, grepping and
    emission under test are the stack's own text.
    """

    ROOT_LOOP = re.compile(r"for candidate in \\\n.*?; do\n", re.DOTALL)

    def _provenance_block(self, stack):
        command = command_text(service_of(stack, API_INIT_SERVICE)).replace("$$", "$")
        start = command.index('API_ROOT=""')
        end_marker = "# --- 2. Version-aware seed: controllers ---" if stack is VPN_STACK else 'CTRL_SRC=""'
        return command[start:command.index(end_marker)]

    @staticmethod
    def _path_for_bash(path):
        """Render a path in the form bash's PATH understands.

        On Windows `as_posix()` still yields `C:/...`, and the drive-letter colon
        is a PATH separator -- the entry would silently never be searched. cygpath
        maps it to `/c/...`; on a real Linux host it is absent and unnecessary.
        """
        probe = subprocess.run(
            [BASH, "-c", f'cygpath -u "{path}"'], capture_output=True, text=True
        )
        if probe.returncode == 0 and probe.stdout.strip():
            return probe.stdout.strip()
        return path

    def _md5_only_path(self, tmpdir):
        """Build a PATH exposing md5sum/awk/grep but NOT sha256sum.

        Forces provenance_hash down its md5 branch. Wrapper scripts (not copies)
        so the real tools do the work and the digest stays trustworthy.
        """
        bindir = Path(tmpdir) / "shim-bin"
        bindir.mkdir()
        for tool in ("md5sum", "awk", "grep"):
            probe = subprocess.run([BASH, "-c", f"command -v {tool}"], capture_output=True, text=True)
            if probe.returncode != 0 or not probe.stdout.strip():
                self.skipTest(f"{tool} unavailable; cannot build an md5-only PATH")
            wrapper = bindir / tool
            wrapper.write_text(
                f'#!/bin/sh\nexec "{probe.stdout.strip()}" "$@"\n', encoding="utf-8", newline="\n"
            )
            wrapper.chmod(0o755)
        shim = self._path_for_bash(bindir.as_posix())
        # The experiment is only meaningful if sha256sum is genuinely unreachable.
        leaked = subprocess.run(
            [BASH, "-c", f'export PATH="{shim}"; command -v sha256sum'],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(
            0, leaked.returncode, "shim PATH still exposes sha256sum; md5 branch unreachable"
        )
        return shim

    def _run(self, stack, fake_root, path_shim=None):
        block = self._provenance_block(stack)
        rewritten, count = self.ROOT_LOOP.subn(f'for candidate in "{fake_root}"; do\n', block)
        self.assertEqual(1, count, f"{stack.name}: candidate-root loop not found to rewrite")
        preamble = f'export PATH="{path_shim}"\n' if path_shim else ""
        script = f'set -eu\nAPI_VERSION=test-fingerprint\n{preamble}{rewritten}\n'
        with tempfile.NamedTemporaryFile(
            "w", suffix=".sh", delete=False, encoding="utf-8", newline="\n"
        ) as handle:
            handle.write(script)
            script_path = handle.name
        try:
            result = subprocess.run(
                [BASH, Path(script_path).as_posix()], capture_output=True, text=True
            )
            self.assertEqual(0, result.returncode, f"{stack.name}: {result.stderr}")
            return result.stdout
        finally:
            Path(script_path).unlink(missing_ok=True)

    def test_provenance_emits_real_hash_and_counts(self):
        import hashlib

        for stack in STACKS:
            with self.subTest(stack=stack.name):
                with tempfile.TemporaryDirectory() as tmp:
                    services = Path(tmp) / "services"
                    services.mkdir()
                    docker_service = b"from services.resume_service import seed_resume_state\nawait seed_resume_state(x)\n"
                    resume_service = b"async def seed_resume_state(\n    arg,\n):\n    pass\n"
                    (services / "docker_service.py").write_bytes(docker_service)
                    (services / "resume_service.py").write_bytes(resume_service)

                    out = self._run(stack, Path(tmp).as_posix())

                    self.assertIn(f"sha256:{hashlib.sha256(docker_service).hexdigest()}", out)
                    self.assertIn(f"sha256:{hashlib.sha256(resume_service).hexdigest()}", out)
                    # 2 references in docker_service.py, 1 definition in resume_service.py
                    self.assertIn("seed_resume_state_refs=2", out)
                    self.assertIn("seed_resume_state_defs=1", out)
                    self.assertIn("image_fingerprint=test-fingerprint", out)

    def test_provenance_falls_back_to_md5_when_sha256sum_unavailable(self):
        # The human's post-merge gate is a log read, so the fallback must emit a
        # real, file-specific digest on an image without coreutils sha256sum --
        # exactly the environment where the sha256 branch cannot cover it.
        import hashlib

        for stack in STACKS:
            with self.subTest(stack=stack.name):
                with tempfile.TemporaryDirectory() as tmp:
                    services = Path(tmp) / "services"
                    services.mkdir()
                    docker_service = b"from services.resume_service import seed_resume_state\n"
                    resume_service = b"async def seed_resume_state(arg):\n    pass\n"
                    (services / "docker_service.py").write_bytes(docker_service)
                    (services / "resume_service.py").write_bytes(resume_service)

                    out = self._run(
                        stack, Path(tmp).as_posix(), path_shim=self._md5_only_path(tmp)
                    )

                    # Digest tied to its file: a fabricated constant fails both.
                    self.assertIn(
                        f"services/docker_service.py md5:{hashlib.md5(docker_service).hexdigest()}",
                        out,
                    )
                    self.assertIn(
                        f"services/resume_service.py md5:{hashlib.md5(resume_service).hexdigest()}",
                        out,
                    )
                    self.assertNotIn("sha256:", out)
                    self.assertNotIn("hash-unavailable", out)

    def test_provenance_warns_when_docker_service_missing_from_image(self):
        # Fail-loud: the human's post-merge gate is a log read, so a missing file
        # must announce itself rather than emit nothing.
        for stack in STACKS:
            with self.subTest(stack=stack.name):
                with tempfile.TemporaryDirectory() as tmp:
                    out = self._run(stack, Path(tmp).as_posix())
                    self.assertIn("[provenance] WARNING", out)
                    self.assertIn("UNVERIFIED", out)


def _resolve_node():
    """Return an absolute path to a working node, or None."""
    candidates = [shutil.which("node"), r"C:\Program Files\nodejs\node.exe", "/usr/bin/node"]
    for candidate in candidates:
        if not candidate or not Path(candidate).exists():
            continue
        try:
            probe = subprocess.run(
                [candidate, "-e", "console.log('hbok')"], capture_output=True, text=True, timeout=60
            )
        except OSError:
            continue
        if probe.returncode == 0 and "hbok" in probe.stdout:
            return candidate
    return None


NODE = _resolve_node()


def gateway_probe_js():
    """The `node -e` program the gateway healthcheck actually runs."""
    test = service_of(VPN_STACK, GATEWAY_SERVICE)["healthcheck"]["test"]
    # ["CMD", "node", "-e", "<program>"]
    return test[test.index("-e") + 1]


class TestGatewayTransportContract(unittest.TestCase):
    """CDX-009 static locks on the gateway transport contract.

    The API derives gateway_use_ssl SOLELY from the URL scheme
    (hummingbot-api/services/gateway_client.py:33) and presents no client cert for
    http://, while docker_service writes gateway_use_ssl=True into every spawned
    bot's conf_client.yml. Plaintext here is an internal contradiction.
    """

    def test_vpn_gateway_url_is_https(self):
        value = env_map(service_of(VPN_STACK, API_SERVICE)).get("GATEWAY_URL")
        self.assertIsNotNone(value, "vpn stack must define GATEWAY_URL for the API")
        self.assertTrue(
            value.lower().startswith("https://"),
            f"GATEWAY_URL must use the https scheme (the config contract states the Gateway "
            f"always runs secured/mTLS, and the scheme is what enables the client cert); got {value!r}",
        )

    def test_vpn_gateway_url_host_matches_a_cert_san(self):
        # The API verifies the server hostname (hummingbot-api/utils/gateway_certs.py:110
        # builds the context with ssl.create_default_context -> check_hostname=True) and the
        # cert set carries DNS SANs only (hummingbot/core/utils/ssl_cert.py:26). An IP
        # literal here raises SSLCertVerificationError on EVERY gateway call, so an https
        # URL alone is not sufficient -- the host must be a name in the SAN set.
        from hummingbot.core.utils.ssl_cert import SAN_DNS

        san_names = {entry.value for entry in SAN_DNS}
        value = env_map(service_of(VPN_STACK, API_SERVICE)).get("GATEWAY_URL")
        host = value.split("://", 1)[1].split(":")[0]
        self.assertIn(
            host,
            san_names,
            f"GATEWAY_URL host {host!r} is not present in the server cert SANs {sorted(san_names)}; "
            f"hostname verification would reject every gateway call.",
        )

    def test_no_vpn_gateway_url_is_explicit_and_fails_fast(self):
        # The no-vpn stack ships no gateway service; leaving GATEWAY_URL unset lets the
        # API fall back to https://localhost:15888, which points at nothing inside the
        # API container -- a silent failure. Explicit non-resolving beats silent default.
        parsed = load_stack(NO_VPN_STACK)
        self.assertNotIn(
            GATEWAY_SERVICE,
            parsed["services"],
            "premise check: the no-vpn stack is expected to ship no gateway service",
        )
        value = env_map(parsed["services"][API_SERVICE]).get("GATEWAY_URL")
        self.assertIsNotNone(value, "no-vpn stack must set GATEWAY_URL explicitly, not rely on the default")
        parts = urlsplit(value)
        # Scheme is load-bearing, not decoration: the API derives gateway_use_ssl
        # from it alone, so an http:// value here would silently define a plaintext
        # gateway contract in a stack whose bots are configured for mTLS.
        self.assertEqual(
            "https",
            parts.scheme,
            f"no-vpn GATEWAY_URL must keep the https scheme -- it is what the API reads to decide "
            f"gateway_use_ssl; got {value!r}",
        )
        self.assertTrue(
            parts.hostname.endswith(".invalid"),
            f"no-vpn GATEWAY_URL must name a reserved, guaranteed-non-resolving host (RFC 6761 "
            f"'.invalid') so gateway routes fail fast by design; got {value!r}",
        )
        self.assertNotIn(
            "localhost",
            parts.hostname,
            "no-vpn GATEWAY_URL must not point at localhost -- nothing serves the gateway there",
        )
        self.assertEqual(
            15888,
            parts.port,
            f"no-vpn GATEWAY_URL must keep the canonical gateway port; got {value!r}",
        )


class TestGatewayNetworkContract(unittest.TestCase):
    """CDX-009 step 4: the gateway network dependency, machine-checked.

    The vpn stack joins no external network *by construction*: gateway shares
    gluetun's namespace via ``network_mode``, which compose forbids combining with
    ``networks:``. So the honest declaration is the absence itself -- these tests
    pin it, so anyone who later attaches gateway to a bridge network (or declares a
    top-level network for it to join) must confront the contract comment first,
    instead of the dependency staying implied either way.
    """

    def test_vpn_gateway_shares_the_vpn_namespace_and_joins_no_network(self):
        parsed = load_stack(VPN_STACK)
        gateway = parsed["services"][GATEWAY_SERVICE]
        self.assertEqual(
            "service:gluetun",
            gateway.get("network_mode"),
            "gateway must share gluetun's network namespace -- this is what makes the API's "
            "loopback GATEWAY_URL correct and what forbids a networks: attachment",
        )
        self.assertNotIn(
            "networks",
            gateway,
            "compose rejects `networks:` alongside `network_mode:`; the stack would fail to start",
        )
        self.assertNotIn(
            "networks",
            parsed,
            "the vpn stack declares no top-level networks by contract (every service uses "
            "network_mode). If you are adding one, update the CDX-009 network contract comment "
            "at the head of the stack file and say which service joins it and why.",
        )

    def test_no_vpn_network_is_created_by_this_stack_not_external(self):
        parsed = load_stack(NO_VPN_STACK)
        networks = parsed["networks"]
        self.assertIn("hbnet-us", networks)
        self.assertEqual(
            "hbnet-us",
            networks["hbnet-us"].get("name"),
            "the network needs an explicit unprefixed name so bots spawned from the API's own "
            "compose project can attach to it by that name",
        )
        self.assertNotIn(
            "external",
            networks["hbnet-us"],
            "hbnet-us is created BY this stack; declaring it external would make compose demand "
            "a pre-existing network and fail the deploy",
        )


class TestGatewayHealthcheckIsNotTcpOnly(unittest.TestCase):
    """The probe must authenticate, not merely observe an open port.

    A TCP-only probe read green whenever something was listening, so a broken TLS
    transport still looked healthy. These are static locks; the behavioural proof
    that the probe actually verifies is in TestGatewayProbeBehaviour below.
    """

    def test_probe_speaks_https_and_no_other_transport(self):
        # Banning the exact former spelling is not enough: `require('node:net')` or
        # `require('tls')` are equivalent bare-socket probes that no denylist of
        # literals catches. Allow-list the modules instead -- an https probe needs
        # exactly https (transport) and fs (reading the cert set), nothing else.
        probe = gateway_probe_js()
        required = set(re.findall(r"require\(\s*['\"](?:node:)?([A-Za-z_][\w/.]*)['\"]\s*\)", probe))
        self.assertIn(
            "https",
            required,
            f"probe must dial the gateway with the https module; it requires {sorted(required)}",
        )
        self.assertEqual(
            set(),
            required - {"https", "fs"},
            f"probe may only require https (transport) and fs (cert material); a probe that reaches "
            f"for another transport module is a bare-socket check wearing an https costume. "
            f"Requires: {sorted(required)}",
        )

    def test_probe_presents_client_credentials_and_verifies_server(self):
        # Asserting that 'client_cert.pem' merely APPEARS somewhere proves nothing:
        # node silently ignores unknown option keys, so `clientCertificate:` (or any
        # typo) still mentions the file while never presenting it. Assert each cert
        # file is bound to its ACTIVE https.request option key.
        raw = gateway_probe_js()
        probe = raw.replace(" ", "")
        self.assertIn("require('https')", raw)
        for option, filename in (("ca", "ca_cert.pem"), ("cert", "client_cert.pem"), ("key", "client_key.pem")):
            with self.subTest(option=option):
                self.assertRegex(
                    probe,
                    rf"(?<![A-Za-z]){option}:fs\.readFileSync\([^)]*{re.escape(filename)}[^)]*\)",
                    f"probe must pass {filename} as the active https.request `{option}:` option -- node "
                    f"ignores an unknown key silently, so the material would never reach the handshake",
                )
        self.assertIn("rejectUnauthorized:true", probe)
        self.assertNotIn("rejectUnauthorized:false", probe)


@unittest.skipIf(NODE is None, "no working node available to execute the gateway probe")
class TestGatewayProbeBehaviour(unittest.TestCase):
    """Execute the REAL probe text from the stack against a real mTLS server.

    Static greps cannot tell an authenticating probe from TLS-theatre: a probe with
    `rejectUnauthorized:false` still mentions every cert file by name. So the probe
    is run for real against servers that differ in exactly one property, and only
    the stack's own program text decides the exit code. Only the port and the cert
    directory are rewritten (15888 and /home/gateway/certs cannot exist on a test
    host); every security-relevant option is the stack's.
    """

    PASSPHRASE = "probe-test-passphrase"

    # ---- certificate authority helpers (mirrors hummingbot/core/utils/ssl_cert.py) ----

    @staticmethod
    def _rsa_key():
        from cryptography.hazmat.primitives.asymmetric import rsa

        return rsa.generate_private_key(public_exponent=65537, key_size=2048)

    @classmethod
    def _make_ca(cls, common_name):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        from cryptography.x509.oid import NameOID

        key = cls._rsa_key()
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
        now = datetime.datetime.now(datetime.UTC)
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=365))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256())
        )
        return key, cert

    @classmethod
    def _make_leaf(cls, ca_key, ca_cert, common_name, dns_names):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

        key = cls._rsa_key()
        now = datetime.datetime.now(datetime.UTC)
        builder = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
            .issuer_name(ca_cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=365))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH]),
                critical=False,
            )
        )
        if dns_names:
            # DNS SANs only -- deliberately no IP SAN, matching ssl_cert.py:26 (SAN_DNS).
            builder = builder.add_extension(
                x509.SubjectAlternativeName([x509.DNSName(n) for n in dns_names]), critical=False
            )
        return key, builder.sign(ca_key, hashes.SHA256())

    @staticmethod
    def _write_pem(directory, name, obj, passphrase=None, is_key=False):
        from cryptography.hazmat.primitives import serialization

        path = Path(directory) / name
        if is_key:
            encryption = (
                serialization.BestAvailableEncryption(passphrase.encode())
                if passphrase
                else serialization.NoEncryption()
            )
            data = obj.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=encryption,
            )
        else:
            data = obj.public_bytes(serialization.Encoding.PEM)
        path.write_bytes(data)
        return path

    def _build_cert_set(self, tmp, server_signed_by_trusted_ca=True):
        """Create the /home/gateway/certs equivalent the probe reads."""
        certs = Path(tmp) / "certs"
        certs.mkdir()
        ca_key, ca_cert = self._make_ca("test-ca")

        if server_signed_by_trusted_ca:
            server_ca_key, server_ca_cert = ca_key, ca_cert
        else:
            # A server whose chain the probe's CA does NOT trust (impersonation).
            server_ca_key, server_ca_cert = self._make_ca("rogue-ca")

        server_key, server_cert = self._make_leaf(
            server_ca_key, server_ca_cert, "localhost", ["localhost", "gateway"]
        )
        client_key, client_cert = self._make_leaf(ca_key, ca_cert, "client", None)

        # ca_cert.pem is the CA the PROBE trusts -- always the good one.
        self._write_pem(certs, "ca_cert.pem", ca_cert)
        self._write_pem(certs, "client_cert.pem", client_cert)
        self._write_pem(certs, "client_key.pem", client_key, passphrase=self.PASSPHRASE, is_key=True)

        server_dir = Path(tmp) / "server"
        server_dir.mkdir()
        server_cert_path = self._write_pem(server_dir, "server_cert.pem", server_cert)
        server_key_path = self._write_pem(server_dir, "server_key.pem", server_key, is_key=True)
        # The CA the SERVER uses to verify incoming client certs.
        server_ca_path = self._write_pem(server_dir, "server_ca.pem", ca_cert)
        return certs, server_cert_path, server_key_path, server_ca_path

    # ---- the server under probe ----

    def _start_server(self, cert, key, ca, status=200, plaintext=False, require_client=True):
        seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                try:
                    seen.append(self.connection.getpeercert())
                except (AttributeError, ValueError):
                    seen.append(None)
                body = json.dumps({"status": "ok"}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        if not plaintext:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certfile=str(cert), keyfile=str(key))
            if require_client:
                context.verify_mode = ssl.CERT_REQUIRED
                context.load_verify_locations(cafile=str(ca))
            server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        # addCleanup is LIFO, so these register in reverse of the required order:
        # shutdown() must stop serve_forever BEFORE the socket is closed, else the
        # serving thread selects on a dead socket (WinError 10038).
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 10)
        self.addCleanup(server.shutdown)
        return server.server_port, seen

    def _run_probe(self, port, certs_dir):
        """Run the stack's own probe text, rewritten only for port + cert dir."""
        probe = gateway_probe_js()
        probe, port_subs = re.subn(r"port:15888", f"port:{port}", probe)
        self.assertEqual(1, port_subs, "probe port not found to rewrite")
        probe, dir_subs = re.subn(
            r"'/home/gateway/certs'", f"'{Path(certs_dir).as_posix()}'", probe
        )
        self.assertEqual(1, dir_subs, "probe cert directory not found to rewrite")

        import os

        env = dict(os.environ, GATEWAY_PASSPHRASE=self.PASSPHRASE)
        result = subprocess.run(
            [NODE, "-e", probe], capture_output=True, text=True, timeout=60, env=env
        )
        return result.returncode

    # ---- the experiments ----

    def test_probe_succeeds_against_authenticated_mtls_gateway(self):
        # Baseline: a correct, mutually-authenticated Gateway must read healthy.
        # Also proves the probe really presents its client cert (the server demands
        # one) and that the passphrase-encrypted client key is loadable.
        with tempfile.TemporaryDirectory() as tmp:
            certs, cert, key, ca = self._build_cert_set(tmp)
            port, seen = self._start_server(cert, key, ca)
            self.assertEqual(0, self._run_probe(port, certs), "probe failed against a healthy mTLS gateway")
            self.assertTrue(seen, "server never served the probe's request")
            self.assertIsNotNone(
                seen[0], "probe did not present a client certificate (mTLS not actually exercised)"
            )
            subject = dict(pair for entry in seen[0]["subject"] for pair in entry)
            self.assertEqual("client", subject.get("commonName"))

    def test_probe_fails_against_untrusted_server_certificate(self):
        # THE anti-theatre experiment. A probe with rejectUnauthorized:false (or one
        # that ignores socket.authorized) still connects, still gets 200, and would
        # PASS here. Only genuine server verification fails this.
        with tempfile.TemporaryDirectory() as tmp:
            certs, cert, key, ca = self._build_cert_set(tmp, server_signed_by_trusted_ca=False)
            port, _ = self._start_server(cert, key, ca)
            self.assertEqual(
                1,
                self._run_probe(port, certs),
                "probe accepted a server whose certificate is not signed by the trusted CA -- "
                "it is not verifying the server (TLS-theatre)",
            )

    def test_probe_fails_against_plaintext_gateway(self):
        # The exact regression CDX-009 is about: a plaintext port that a TCP-only
        # probe reported as healthy must now read unhealthy.
        with tempfile.TemporaryDirectory() as tmp:
            certs, cert, key, ca = self._build_cert_set(tmp)
            port, _ = self._start_server(cert, key, ca, plaintext=True)
            self.assertEqual(
                1, self._run_probe(port, certs), "probe reported a plaintext gateway as healthy"
            )

    def test_probe_fails_on_non_2xx_response(self):
        # Locks the status check: exit 0 only on an authenticated 2xx.
        with tempfile.TemporaryDirectory() as tmp:
            certs, cert, key, ca = self._build_cert_set(tmp)
            port, _ = self._start_server(cert, key, ca, status=503)
            self.assertEqual(
                1, self._run_probe(port, certs), "probe reported a 503 gateway as healthy"
            )

    def test_probe_fails_when_nothing_listens(self):
        # Connection refused must not read healthy (guards an unconditional exit 0).
        with tempfile.TemporaryDirectory() as tmp:
            certs, _cert, _key, _ca = self._build_cert_set(tmp)
            self.assertEqual(1, self._run_probe(_closed_port(), certs))


def _closed_port():
    """Bind and immediately release a port so nothing is listening on it."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# ── CDX-010: the controllers destination must be explicit ────────────────────

BOT_BUILD_SCRIPT = REPO_ROOT / "build_hummingbot_nonkyc.sh"
API_BUILD_SCRIPT = REPO_ROOT / "Build_hummingbot_api_nonkyc.sh"
BUILD_SCRIPTS = (BOT_BUILD_SCRIPT, API_BUILD_SCRIPT)

KNOWN_TREE_VPN = "/mnt/sharedrive/apps/hummingbot/api/data/bots/controllers"
KNOWN_TREE_NO_VPN = "/mnt/sharedrive/apps/hummingbot_us/api/data/bots/controllers"
MANIFEST_NAME = "controllers.manifest.sha256"


def extract_block_function(text, name):
    """Return the source of a multi-line `name() { ... }` shell function.

    Relies on the closing brace being at column 0, which is this codebase's
    style for every function in both build scripts.
    """
    match = re.search(rf"^{re.escape(name)}\(\) \{{\n.*?^\}}$", text, re.M | re.S)
    if match is None:
        raise AssertionError(f"shell function {name}() not found")
    return match.group(0)


def extract_oneline_function(text, name):
    """Return the source of a single-line `name() { ...; }` shell helper."""
    match = re.search(rf"^{re.escape(name)}\(\)\s*\{{.*\}}$", text, re.M)
    if match is None:
        raise AssertionError(f"shell helper {name}() not found")
    return match.group(0)


def extract_assignment(text, name):
    """Return the source of a top-level `NAME=...` assignment line."""
    match = re.search(rf"^{re.escape(name)}=.*$", text, re.M)
    if match is None:
        raise AssertionError(f"assignment {name}= not found")
    return match.group(0)


def line_number_of(text, pattern):
    """1-indexed line of the first regex match, or None."""
    match = re.search(pattern, text, re.M)
    if match is None:
        return None
    return text[: match.start()].count("\n") + 1


class TestControllersDestinationIsExplicit(unittest.TestCase):
    """CDX-010 static locks: no default destination, and the guard runs first.

    The triage corrected the report here: the old default did not make a
    forgotten `--controllers-dest` *skip*. The VPN tree exists on the same share
    as the no-VPN tree, so a defaulted no-VPN build SUCCEEDED into the wrong
    stack's live controllers, silently. Hence: no default at all.
    """

    def test_controllers_dest_has_no_default_value(self):
        # Exact-equality on the assignment, not a substring hunt: any default --
        # the old /mnt VPN tree or a new one -- makes this differ. `${VAR:-}` keeps
        # an explicit CONTROLLERS_DEST env var working while removing the fallback.
        for script in BUILD_SCRIPTS:
            with self.subTest(script=script.name):
                self.assertEqual(
                    'CONTROLLERS_DEST="${CONTROLLERS_DEST:-}"',
                    extract_assignment(read_text(script), "CONTROLLERS_DEST"),
                    f"{script.name}: CONTROLLERS_DEST must have NO default; a default is what "
                    f"let a no-VPN build sync into the VPN stack's tree.",
                )

    # Every line permitted to assign CONTROLLERS_DEST. An allow-list, not a
    # denylist of known-bad spellings: a default reintroduced as
    # `CONTROLLERS_DEST="${CONTROLLERS_DEST:-/mnt/...}"`, as a bare
    # `CONTROLLERS_DEST=/mnt/...`, or as a late re-assignment after the guard has
    # already run, is a line that is not in this set.
    ALLOWED_DEST_ASSIGNMENTS = {
        'CONTROLLERS_DEST="${CONTROLLERS_DEST:-}"',
        '--controllers-dest)   CONTROLLERS_DEST="$2"; shift 2 ;;',
        '--controllers-dest=*) CONTROLLERS_DEST="${1#*=}"; shift ;;',
    }

    def test_no_defaulted_destination_anywhere_in_either_script(self):
        for script in BUILD_SCRIPTS:
            with self.subTest(script=script.name):
                assignments = [
                    line.strip()
                    for line in read_text(script).splitlines()
                    if re.search(r"(?<![\w-])CONTROLLERS_DEST=", line) and not line.strip().startswith("#")
                ]
                self.assertTrue(assignments, "premise check: CONTROLLERS_DEST is assigned somewhere")
                unexpected = set(assignments) - self.ALLOWED_DEST_ASSIGNMENTS
                self.assertEqual(
                    set(),
                    unexpected,
                    f"{script.name}: unreviewed assignment(s) to the controllers destination: "
                    f"{sorted(unexpected)}. CDX-010 allows it to be set only by "
                    f"--controllers-dest / an explicit env var -- never defaulted.",
                )

    def test_help_text_does_not_advertise_a_default(self):
        for script in BUILD_SCRIPTS:
            with self.subTest(script=script.name):
                text = read_text(script)
                self.assertNotIn(
                    'echo "                          (default: $CONTROLLERS_DEST)"',
                    text,
                    f"{script.name}: --help still claims a default destination",
                )
                self.assertIn("REQUIRED unless --no-controllers-sync", text)

    def test_controllers_dest_flag_parsing_still_works(self):
        # The fix removes a default, not the flag. Both spellings must survive.
        # re.M is load-bearing: these are line-anchored patterns and assertRegex
        # uses re.search, so without it `^` would only match the file's first line.
        for script in BUILD_SCRIPTS:
            with self.subTest(script=script.name):
                text = read_text(script)
                for pattern in (
                    r'^\s*--controllers-dest\)\s+CONTROLLERS_DEST="\$2"; shift 2 ;;$',
                    r'^\s*--controllers-dest=\*\)\s+CONTROLLERS_DEST="\$\{1#\*=\}"; shift ;;$',
                    r'^\s*--no-controllers-sync\)\s+SYNC_CONTROLLERS=0; shift ;;$',
                ):
                    self.assertRegex(text, re.compile(pattern, re.M))

    def test_validation_precedes_the_purge_and_every_docker_build(self):
        # The ordering IS the fix: `exit 1` after the purge has already stopped and
        # removed live bot containers is not a fail-fast, it is an outage. Compares
        # real line numbers rather than asserting the guard merely exists.
        for script in BUILD_SCRIPTS:
            with self.subTest(script=script.name):
                text = read_text(script)
                # Column 0 => a top-level call in Main, not nested in a conditional
                # that could skip it.
                call = line_number_of(text, r"^validate_controllers_dest$")
                self.assertIsNotNone(
                    call, f"{script.name}: validate_controllers_dest is never called at top level"
                )
                purge = line_number_of(text, r"^\s*purge_old_image_and_containers \\$")
                self.assertIsNotNone(purge, f"{script.name}: purge call not found")
                build = line_number_of(text, r"^docker build ")
                self.assertIsNotNone(build, f"{script.name}: docker build not found")
                self.assertLess(
                    call,
                    purge,
                    f"{script.name}: the destination check (line {call}) must run BEFORE the purge "
                    f"(line {purge}) -- the purge stops and REMOVES live bot containers.",
                )
                self.assertLess(
                    call,
                    build,
                    f"{script.name}: the destination check (line {call}) must run BEFORE the first "
                    f"docker build (line {build}).",
                )

    def test_sync_cannot_silently_skip_a_missing_source_or_destination(self):
        # The warn-and-return-0 guards were the silent-success path itself.
        for script in BUILD_SCRIPTS:
            with self.subTest(script=script.name):
                body = extract_block_function(read_text(script), "sync_controllers")
                self.assertNotIn(
                    "skipping controller sync",
                    body,
                    f"{script.name}: sync_controllers still has a warn-and-skip path; a build that "
                    f"syncs nothing must fail, not report success.",
                )
                # Exactly one survivor: the explicit --no-controllers-sync opt-out.
                self.assertEqual(
                    1,
                    len(re.findall(r"^\s*return 0$", body, re.M)),
                    f"{script.name}: sync_controllers must have exactly one non-fatal exit "
                    f"(the explicit --no-controllers-sync opt-out).",
                )


class TestControllersProvenanceRecorded(unittest.TestCase):
    """CDX-010 provenance: manifest at the destination, hashes on the image."""

    def test_manifest_is_written_into_the_destination(self):
        for script in BUILD_SCRIPTS:
            with self.subTest(script=script.name):
                text = read_text(script)
                self.assertEqual(
                    f'CONTROLLERS_MANIFEST_NAME="{MANIFEST_NAME}"',
                    extract_assignment(text, "CONTROLLERS_MANIFEST_NAME"),
                )
                sync = extract_block_function(text, "sync_controllers")
                # Bound to $dest: a manifest written anywhere else records the
                # synced set where nothing will ever read it.
                self.assertIn('local manifest_path="$dest/$CONTROLLERS_MANIFEST_NAME"', sync)
                self.assertIn(
                    """printf '%s\\n' "$CONTROLLERS_MANIFEST_BODY" > "$manifest_path\"""",
                    sync,
                )

    def test_both_provenance_labels_are_on_the_final_docker_build(self):
        # Anchored to the label flag AND to the variable that carries the value: a
        # label whose value is a literal, or a stale/unset var, is not provenance.
        expected_commit_var = {BOT_BUILD_SCRIPT: "$HB_SHA_FULL", API_BUILD_SCRIPT: "$HBOT_COMMIT_SHA"}
        for script in BUILD_SCRIPTS:
            with self.subTest(script=script.name):
                text = read_text(script)
                # The LAST docker build invocation is the deployed image.
                final_build = text[text.rindex("docker build $DOCKER_BUILD_FLAGS"):]
                final_build = final_build[: final_build.index('\n\n')]
                self.assertIn(
                    f'--label "nonkyc.hummingbot_commit={expected_commit_var[script]}"',
                    final_build,
                    f"{script.name}: the final image must carry the hummingbot commit label",
                )
                self.assertIn(
                    '--label "nonkyc.controllers_manifest_sha256=$CONTROLLERS_MANIFEST_SHA256"',
                    final_build,
                    f"{script.name}: the final image must carry the controllers manifest label",
                )

    def test_manifest_is_computed_before_the_build_that_labels_it(self):
        # A hash computed after the build could not have been stamped on it; a hash
        # computed from a re-fetch could describe a different commit than the label.
        for script in BUILD_SCRIPTS:
            with self.subTest(script=script.name):
                text = read_text(script)
                compute = line_number_of(text, r"^\s*compute_controllers_manifest \"")
                self.assertIsNotNone(compute, f"{script.name}: manifest is never computed")
                final_build = text.rindex("docker build $DOCKER_BUILD_FLAGS")
                final_build_line = text[:final_build].count("\n") + 1
                self.assertLess(
                    compute,
                    final_build_line,
                    f"{script.name}: the manifest (line {compute}) must be computed before the "
                    f"final docker build (line {final_build_line}) that stamps its hash.",
                )

    def test_api_script_fetches_controllers_once_before_the_build(self):
        # API-specific: the fetch moved ahead of the build so the labelled tree and
        # the synced tree are the same checkout. A second fetch afterwards would
        # re-clone at HEAD and could sync a commit the label does not name.
        text = read_text(API_BUILD_SCRIPT)
        fetches = re.findall(r"^\s*fetch_controllers_source$", text, re.M)
        self.assertEqual(
            1, len(fetches), "the API script must fetch the controllers source exactly once"
        )
        fetch_line = line_number_of(text, r"^\s*fetch_controllers_source$")
        build_line = text[: text.rindex("docker build $DOCKER_BUILD_FLAGS")].count("\n") + 1
        sync_line = line_number_of(text, r"^\s*sync_controllers \"\$HBOT_CONTROLLERS_SRC\"$")
        self.assertLess(fetch_line, build_line, "fetch must precede the build it labels")
        self.assertLess(build_line, sync_line, "the sync stays post-build")


def _harness(script_path, functions, preamble="", body=""):
    """Assemble a runnable script from the build script's OWN function text.

    Only the named functions are extracted -- never the script's Main flow -- so
    nothing here can clone, purge, or invoke docker. The `docker` poison pill and
    the assertion in _run_harness enforce that rather than trusting it.
    """
    text = read_text(script_path)
    parts = ["set -uo pipefail", 'docker() { echo "FATAL: extracted block invoked docker" >&2; exit 111; }']
    for name in ("log", "warn", "die", "ok"):
        # The REAL helpers: `die` is the unit under test's failure mechanism (it
        # is what exits 1), so stubbing it would be testing the stub.
        parts.append(extract_oneline_function(text, name))
    for name in ("KNOWN_CONTROLLERS_TREE_VPN", "KNOWN_CONTROLLERS_TREE_NO_VPN", "CONTROLLERS_MANIFEST_NAME"):
        parts.append(extract_assignment(text, name))
    parts.append(preamble)
    for name in functions:
        parts.append(extract_block_function(text, name))
    parts.append(body)
    return "\n".join(parts) + "\n"


@unittest.skipIf(BASH is None, "no working bash available to execute build-script functions")
class TestControllersDestValidationBehaviour(unittest.TestCase):
    """Execute the REAL validate_controllers_dest() text from each build script.

    Greps cannot distinguish a guard that fires from one that is dead: an error
    message the code never reaches still greps green. So the script's own function
    text decides the exit code here. Extraction is limited to the function under
    test -- the Main flow (clone/purge/docker build) is never assembled.
    """

    def _run(self, script_path, dest=None, sync=1, dest_is_env_unset=False):
        preamble = f"SYNC_CONTROLLERS={sync}"
        if dest_is_env_unset:
            # The real post-arg-parse state when --controllers-dest is omitted.
            preamble += "\n" + extract_assignment(read_text(script_path), "CONTROLLERS_DEST")
        else:
            preamble += f'\nCONTROLLERS_DEST="{dest}"'
        harness = _harness(
            script_path, ["validate_controllers_dest"], preamble, "validate_controllers_dest"
        )
        self.assertNotIn(
            "docker build",
            harness,
            "harness must never assemble a docker invocation from the build script",
        )
        with tempfile.NamedTemporaryFile(
            "w", suffix=".sh", delete=False, encoding="utf-8", newline="\n"
        ) as handle:
            handle.write(harness)
            path = handle.name
        try:
            result = subprocess.run(
                [BASH, Path(path).as_posix()], capture_output=True, text=True, timeout=60
            )
            self.assertNotEqual(111, result.returncode, "extracted block invoked docker")
            return result
        finally:
            Path(path).unlink(missing_ok=True)

    def test_unset_destination_fails_the_build(self):
        # THE finding: this used to be a silent success into the VPN tree.
        for script in BUILD_SCRIPTS:
            with self.subTest(script=script.name):
                result = self._run(script, dest_is_env_unset=True)
                self.assertNotEqual(
                    0,
                    result.returncode,
                    f"{script.name}: an unset controllers destination must FAIL the build; "
                    f"stdout={result.stdout!r}",
                )

    def test_failure_message_names_the_flag_and_both_known_trees(self):
        # The error has to be actionable: the operator's next keystroke must be in
        # it. Both trees, because naming only one recreates the default's bias.
        for script in BUILD_SCRIPTS:
            with self.subTest(script=script.name):
                message = self._run(script, dest_is_env_unset=True).stderr
                self.assertIn("--controllers-dest", message)
                self.assertIn("--no-controllers-sync", message)
                self.assertIn(KNOWN_TREE_VPN, message)
                self.assertIn(KNOWN_TREE_NO_VPN, message)

    def test_nonexistent_destination_fails_the_build(self):
        for script in BUILD_SCRIPTS:
            with self.subTest(script=script.name):
                with tempfile.TemporaryDirectory() as tmp:
                    missing = Path(tmp) / "no-such-tree" / "controllers"
                    result = self._run(script, dest=missing.as_posix())
                    self.assertNotEqual(
                        0,
                        result.returncode,
                        f"{script.name}: a destination that does not exist must fail the build",
                    )
                    self.assertIn(missing.as_posix(), result.stderr)

    def test_destination_that_is_a_file_fails_the_build(self):
        # `-d`, not `-e`: syncing into a regular path would explode mid-copy, after
        # the purge has already taken the containers down.
        for script in BUILD_SCRIPTS:
            with self.subTest(script=script.name):
                with tempfile.TemporaryDirectory() as tmp:
                    not_a_dir = Path(tmp) / "controllers"
                    not_a_dir.write_text("i am a file", encoding="utf-8")
                    self.assertNotEqual(0, self._run(script, dest=not_a_dir.as_posix()).returncode)

    def test_existing_directory_is_accepted(self):
        # The accept side: the guard must not be a blanket refusal (which would
        # "pass" every rejection test above while breaking every real build).
        for script in BUILD_SCRIPTS:
            with self.subTest(script=script.name):
                with tempfile.TemporaryDirectory() as tmp:
                    result = self._run(script, dest=Path(tmp).as_posix())
                    self.assertEqual(
                        0, result.returncode, f"{script.name}: a real directory must be accepted"
                    )

    def test_no_controllers_sync_still_skips_without_a_destination(self):
        # The documented escape hatch must survive: explicit opt-out, no dest.
        for script in BUILD_SCRIPTS:
            with self.subTest(script=script.name):
                result = self._run(script, sync=0, dest_is_env_unset=True)
                self.assertEqual(
                    0,
                    result.returncode,
                    f"{script.name}: --no-controllers-sync must remain a valid way to skip",
                )


@unittest.skipIf(BASH is None, "no working bash available to execute build-script functions")
class TestControllersManifestBehaviour(unittest.TestCase):
    """Execute the REAL manifest functions against a synthetic controllers tree.

    Every expected value is derived from the CDX-010 spec ("sorted
    `sha256<2 spaces>relative/path` lines over the synced set") and computed
    independently in Python -- never captured by running the implementation.
    """

    FILES = {
        "zzz_last.py": b"z = 1\n",
        "aaa_first.py": b"a = 1\n",
        "market_making/range_inventory_ladder.py": b"class Ladder:\n    pass\n",
        "market_making/__init__.py": b"",
    }
    EXCLUDED = {
        "__pycache__/aaa_first.cpython-310.pyc": b"compiled",
        "market_making/__pycache__/cached.py": b"cached = 1\n",
        "README.md": b"not python\n",
    }

    def _make_tree(self, root):
        src = Path(root) / "controllers"
        for rel, data in {**self.FILES, **self.EXCLUDED}.items():
            path = src / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return src

    def _expected_manifest(self):
        """The manifest the SPEC requires, computed here, not observed."""
        import hashlib

        lines = [
            f"{hashlib.sha256(data).hexdigest()}  {rel}"
            for rel, data in sorted(self.FILES.items())
        ]
        return "\n".join(lines) + "\n"

    def _compute(self, script_path, src, out):
        harness = _harness(
            script_path,
            ["controllers_manifest_body", "compute_controllers_manifest"],
            "",
            f'compute_controllers_manifest "{src}"\n'
            f'printf \'%s\\n\' "$CONTROLLERS_MANIFEST_BODY" > "{out}/body.txt"\n'
            f'printf \'%s\' "$CONTROLLERS_MANIFEST_SHA256" > "{out}/sha.txt"\n',
        )
        with tempfile.NamedTemporaryFile(
            "w", suffix=".sh", delete=False, encoding="utf-8", newline="\n"
        ) as handle:
            handle.write(harness)
            path = handle.name
        try:
            result = subprocess.run(
                [BASH, Path(path).as_posix()], capture_output=True, text=True, timeout=120
            )
            self.assertEqual(0, result.returncode, f"manifest computation failed: {result.stderr}")
            return (
                (Path(out) / "body.txt").read_text(encoding="utf-8"),
                (Path(out) / "sha.txt").read_text(encoding="utf-8").strip(),
            )
        finally:
            Path(path).unlink(missing_ok=True)

    def test_manifest_matches_the_spec_format_exactly(self):
        import hashlib

        for script in BUILD_SCRIPTS:
            with self.subTest(script=script.name):
                with tempfile.TemporaryDirectory() as tmp:
                    src = self._make_tree(tmp)
                    body, sha = self._compute(script, src.as_posix(), Path(tmp).as_posix())

                    expected = self._expected_manifest()
                    # Byte-exact: digest, TWO spaces, forward-slash relative path,
                    # ordered by path. A `sha256sum <path>` implementation emits
                    # "<hash> *<path>" on a binary-mode host and fails right here.
                    self.assertEqual(expected, body)
                    self.assertEqual(hashlib.sha256(expected.encode()).hexdigest(), sha)

    def test_manifest_covers_exactly_the_synced_set(self):
        # The manifest must describe what sync_controllers copies -- the same find
        # predicate -- or it attests to files that never reached the destination.
        for script in BUILD_SCRIPTS:
            with self.subTest(script=script.name):
                with tempfile.TemporaryDirectory() as tmp:
                    src = self._make_tree(tmp)
                    body, _ = self._compute(script, src.as_posix(), Path(tmp).as_posix())
                    listed = {line.split("  ", 1)[1] for line in body.strip().splitlines()}
                    self.assertEqual(set(self.FILES), listed)
                    for excluded in self.EXCLUDED:
                        self.assertNotIn(excluded, listed)

    def test_manifest_hash_changes_when_a_controller_changes(self):
        # Provenance that cannot detect a change is decoration. One byte in one
        # file must move the label the image is stamped with.
        for script in BUILD_SCRIPTS:
            with self.subTest(script=script.name):
                with tempfile.TemporaryDirectory() as tmp:
                    src = self._make_tree(tmp)
                    _, before = self._compute(script, src.as_posix(), Path(tmp).as_posix())
                    (src / "market_making/range_inventory_ladder.py").write_bytes(
                        b"class Ladder:\n    pass  # edited\n"
                    )
                    _, after = self._compute(script, src.as_posix(), Path(tmp).as_posix())
                    self.assertNotEqual(before, after)

    def test_manifest_hash_is_stable_across_runs_and_paths(self):
        # Same content in a different build dir => same hash, or the label is
        # noise and "did the controllers change?" becomes unanswerable.
        for script in BUILD_SCRIPTS:
            with self.subTest(script=script.name):
                with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
                    _, first = self._compute(
                        script, self._make_tree(one).as_posix(), Path(one).as_posix()
                    )
                    _, second = self._compute(
                        script, self._make_tree(two).as_posix(), Path(two).as_posix()
                    )
                    self.assertEqual(first, second)

    def test_empty_controllers_tree_fails_rather_than_labelling_a_lie(self):
        # An empty set hashes to a perfectly valid-looking constant. Stamping that
        # on an image claims provenance for controllers that were never there.
        for script in BUILD_SCRIPTS:
            with self.subTest(script=script.name):
                with tempfile.TemporaryDirectory() as tmp:
                    empty = Path(tmp) / "controllers"
                    empty.mkdir()
                    harness = _harness(
                        script,
                        ["controllers_manifest_body", "compute_controllers_manifest"],
                        "",
                        f'compute_controllers_manifest "{empty.as_posix()}"',
                    )
                    with tempfile.NamedTemporaryFile(
                        "w", suffix=".sh", delete=False, encoding="utf-8", newline="\n"
                    ) as handle:
                        handle.write(harness)
                        path = handle.name
                    try:
                        result = subprocess.run(
                            [BASH, Path(path).as_posix()], capture_output=True, text=True, timeout=60
                        )
                        self.assertNotEqual(
                            0, result.returncode, "an empty controllers tree must fail the build"
                        )
                    finally:
                        Path(path).unlink(missing_ok=True)


@unittest.skipIf(BASH is None, "no working bash available to execute build-script functions")
class TestControllersSyncWritesManifest(unittest.TestCase):
    """Execute the REAL sync_controllers() against synthetic src/dest trees.

    Proves the manifest reaches the destination with the content the image label
    attests to -- the property the CDX-010 provenance requirement is actually
    about. Nothing docker-adjacent is assembled; only the function under test.
    """

    def _sync(self, script_path, src, dest):
        harness = _harness(
            script_path,
            ["controllers_manifest_body", "compute_controllers_manifest", "sync_controllers"],
            f'SYNC_CONTROLLERS=1\nCONTROLLERS_DEST="{dest}"\n'
            f'CONTROLLERS_MANIFEST_SHA256="unknown"\nCONTROLLERS_MANIFEST_BODY=""',
            f'sync_controllers "{src}"',
        )
        self.assertNotIn("docker build", harness)
        with tempfile.NamedTemporaryFile(
            "w", suffix=".sh", delete=False, encoding="utf-8", newline="\n"
        ) as handle:
            handle.write(harness)
            path = handle.name
        try:
            result = subprocess.run(
                [BASH, Path(path).as_posix()], capture_output=True, text=True, timeout=120
            )
            self.assertNotEqual(111, result.returncode, "extracted block invoked docker")
            return result
        finally:
            Path(path).unlink(missing_ok=True)

    def test_sync_writes_a_manifest_matching_the_files_it_copied(self):
        import hashlib

        for script in BUILD_SCRIPTS:
            with self.subTest(script=script.name):
                with tempfile.TemporaryDirectory() as tmp:
                    src = Path(tmp) / "src"
                    (src / "market_making").mkdir(parents=True)
                    top = b"a = 1\n"
                    nested = b"b = 2\n"
                    (src / "aaa.py").write_bytes(top)
                    (src / "market_making/ladder.py").write_bytes(nested)
                    dest = Path(tmp) / "dest"
                    dest.mkdir()

                    result = self._sync(script, src.as_posix(), dest.as_posix())
                    self.assertEqual(0, result.returncode, result.stderr)

                    # The controllers themselves landed...
                    self.assertEqual(top, (dest / "aaa.py").read_bytes())
                    self.assertEqual(nested, (dest / "market_making/ladder.py").read_bytes())
                    # ...and the manifest describes exactly them, per the spec.
                    # Digests are computed outside the f-string: a `\n` escape inside
                    # an f-string expression is a literal backslash-n, which would
                    # hash the wrong bytes and make this assertion a lie.
                    manifest = (dest / MANIFEST_NAME).read_text(encoding="utf-8")
                    expected = (
                        f"{hashlib.sha256(top).hexdigest()}  aaa.py\n"
                        f"{hashlib.sha256(nested).hexdigest()}  market_making/ladder.py\n"
                    )
                    self.assertEqual(expected, manifest)

    def test_manifest_is_written_even_when_no_controller_changed(self):
        # The no-op path is the common case (rebuild without strategy edits). A
        # destination that predates this feature must still gain its manifest.
        for script in BUILD_SCRIPTS:
            with self.subTest(script=script.name):
                with tempfile.TemporaryDirectory() as tmp:
                    src = Path(tmp) / "src"
                    src.mkdir()
                    (src / "aaa.py").write_bytes(b"a = 1\n")
                    dest = Path(tmp) / "dest"
                    dest.mkdir()
                    # Destination already byte-identical => changed+added == 0.
                    (dest / "aaa.py").write_bytes(b"a = 1\n")

                    result = self._sync(script, src.as_posix(), dest.as_posix())
                    self.assertEqual(0, result.returncode, result.stderr)
                    self.assertIn("already up to date", result.stdout)
                    self.assertTrue(
                        (dest / MANIFEST_NAME).is_file(),
                        "manifest must be written even when nothing changed",
                    )

    def test_replaced_manifest_is_backed_up_like_any_other_file(self):
        # The destination's stated contract is that nothing is ever destroyed.
        for script in BUILD_SCRIPTS:
            with self.subTest(script=script.name):
                with tempfile.TemporaryDirectory() as tmp:
                    src = Path(tmp) / "src"
                    src.mkdir()
                    (src / "aaa.py").write_bytes(b"a = 2\n")
                    dest = Path(tmp) / "dest"
                    dest.mkdir()
                    (dest / "aaa.py").write_bytes(b"a = 1\n")
                    (dest / MANIFEST_NAME).write_text("stale manifest\n", encoding="utf-8")

                    self.assertEqual(0, self._sync(script, src.as_posix(), dest.as_posix()).returncode)

                    backups = list(dest.glob(f".backup-*/{MANIFEST_NAME}"))
                    self.assertEqual(
                        1, len(backups), f"replaced manifest must be backed up; found {backups}"
                    )
                    self.assertEqual("stale manifest\n", backups[0].read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
