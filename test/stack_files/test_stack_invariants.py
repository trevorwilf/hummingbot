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


if __name__ == "__main__":
    unittest.main()
