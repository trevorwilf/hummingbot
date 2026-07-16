"""Invariants for the deployment stack files (docker_files/).

CDX-002: the api service used to bind-mount a host-patched copy of
``services/docker_service.py`` over the image's own file. The patched copy was
generated once, on first boot, and then retained forever -- so rebuilt api code
(including the copy-forward hook wiring at docker_service.py:15/:309) never ran.
These tests lock the cutover: no mount, no patch machinery, provenance emitted,
and the env vars the patch used to supply are pinned explicitly.

Pure file-content tests. Nothing here runs, builds, or inspects docker.
"""

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

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
    """Boot provenance makes runtime-source-vs-image verification a log read."""

    def test_api_init_emits_provenance_for_docker_service(self):
        for stack in STACKS:
            with self.subTest(stack=stack.name):
                command = command_text(service_of(stack, API_INIT_SERVICE))
                self.assertIn("[provenance]", command)
                self.assertIn("services/docker_service.py", command)
                self.assertIn("sha256sum", command)
                self.assertIn("md5sum", command)

    def test_provenance_reports_seed_resume_state_counts(self):
        # seed_resume_state is DEFINED in services/resume_service.py:1462 and
        # imported/called by docker_service.py:15/:309 -- the wiring the retained
        # patch used to shadow. Provenance attests to both files.
        for stack in STACKS:
            with self.subTest(stack=stack.name):
                command = command_text(service_of(stack, API_INIT_SERVICE))
                self.assertIn("seed_resume_state_defs=", command)
                self.assertIn("seed_resume_state_refs=", command)
                self.assertIn("services/resume_service.py", command)


class TestApiInitUnrelatedDutiesPreserved(unittest.TestCase):
    """Removal had to be surgical: api-init's seeding duties must survive."""

    def test_version_aware_seeds_still_present(self):
        for stack in STACKS:
            with self.subTest(stack=stack.name):
                command = command_text(service_of(stack, API_INIT_SERVICE))
                self.assertIn("seed_helpers.sh", command)
                self.assertIn("fingerprint_dir", command)
                for destination in ("controllers", "scripts", "conf"):
                    self.assertIn(destination, command)
                self.assertGreaterEqual(command.count("version_aware_seed"), 3)


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
        expected = {VPN_STACK: "none", NO_VPN_STACK: "hbnet-us"}
        for stack in STACKS:
            with self.subTest(stack=stack.name):
                value = env_map(service_of(stack, API_SERVICE)).get("DOCKER_BOT_NETWORK_MODE")
                self.assertIsNotNone(value, f"{stack.name}: DOCKER_BOT_NETWORK_MODE not pinned")
                self.assertTrue(
                    value.startswith("${DOCKER_BOT_NETWORK_MODE:-"),
                    f"{stack.name}: expected an explicit `:-` fallback, got {value!r}",
                )
                self.assertIn(expected[stack], value)

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
        # _get_compose_labels adds the compose labels only when this is non-empty.
        for stack in STACKS:
            with self.subTest(stack=stack.name):
                value = env_map(service_of(stack, API_SERVICE)).get("COMPOSE_PROJECT_NAME")
                self.assertTrue(value, f"{stack.name}: COMPOSE_PROJECT_NAME must be non-empty")


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

    def _run(self, stack, fake_root):
        block = self._provenance_block(stack)
        rewritten, count = self.ROOT_LOOP.subn(f'for candidate in "{fake_root}"; do\n', block)
        self.assertEqual(1, count, f"{stack.name}: candidate-root loop not found to rewrite")
        script = f'set -eu\nAPI_VERSION=test-fingerprint\n{rewritten}\n'
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

    def test_provenance_warns_when_docker_service_missing_from_image(self):
        # Fail-loud: the human's post-merge gate is a log read, so a missing file
        # must announce itself rather than emit nothing.
        for stack in STACKS:
            with self.subTest(stack=stack.name):
                with tempfile.TemporaryDirectory() as tmp:
                    out = self._run(stack, Path(tmp).as_posix())
                    self.assertIn("[provenance] WARNING", out)
                    self.assertIn("UNVERIFIED", out)


if __name__ == "__main__":
    unittest.main()
