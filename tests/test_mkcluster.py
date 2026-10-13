"""Hermetic regression tests: no real containers, Kubernetes, or network access."""

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "mkcluster"
BASH = shutil.which("bash")
GPU_UUID = "GPU-12345678-1234-1234-1234-123456789abc"
NODE_IMAGE = "kindest/node:v1.35.5@sha256:" + "a" * 64

# Every potentially mutating/networked dependency is replaced. Unconfigured calls
# fail closed, and all calls record argv boundaries (including paths with spaces).
STUB = """#!PYTHON
import json
import os
from pathlib import Path
import sys

name = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ['MKCLUSTER_TEST_LOG'], 'a') as log:
    log.write(json.dumps({'name': name, 'args': args,
                         'provider': os.environ.get('KIND_EXPERIMENTAL_PROVIDER'),
                         'kubeconfig': os.environ.get('KUBECONFIG')}) + '\\n')
rules = json.loads(Path(os.environ['MKCLUSTER_TEST_RESPONSES']).read_text())
for rule in rules.get(name, []):
    prefix = rule.get('prefix', [])
    if args[:len(prefix)] != prefix:
        continue
    if not all(value in args for value in rule.get('contains', [])):
        continue
    if not all(any(value in arg for arg in args) for value in rule.get('fragments', [])):
        continue
    print(rule.get('stdout', ''), end='')
    print(rule.get('stderr', ''), end='', file=sys.stderr)
    if rule.get('read_stdin'):
        sys.stdin.read()
    sys.exit(rule.get('code', 0))
print('Unconfigured test command: ' + name + ' ' + repr(args), file=sys.stderr)
sys.exit(97)
""".replace("PYTHON", sys.executable, 1)


class MkclusterTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mkcluster-test-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.bin = self.directory / "bin"
        self.bin.mkdir()
        self.log = self.directory / "commands.jsonl"
        self.responses = self.directory / "responses.json"
        self.rules = {}
        self.responses.write_text("{}")
        self.env = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("MKCLUSTER_", "KIND_"))
            and key not in ("KUBECONFIG", "CONTAINER_CLI", "BASH_ENV", "ENV")
        }
        self.env.update({
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "TMPDIR": str(self.directory),
            "MKCLUSTER_TEST_LOG": str(self.log),
            "MKCLUSTER_TEST_RESPONSES": str(self.responses),
        })
        self.pinned_env = {
            "MKCLUSTER_KIND_VERSION": "v0.31.0",
            "MKCLUSTER_KIND_NODE_IMAGE": NODE_IMAGE,
        }
        for name in (
            "curl", "wget", "docker", "nerdctl", "podman", "kind", "kubectl",
            "helm", "nvidia-smi", "nvidia-container-runtime", "ldconfig",
            "sudo", "go", "brew", "sysctl", "systemctl",
        ):
            path = self.bin / name
            path.write_text(STUB)
            path.chmod(0o755)

    def respond(self, name, **rule):
        self.rules.setdefault(name, []).append(rule)
        self.responses.write_text(json.dumps(self.rules))

    def run_cli(self, *args, env=None):
        return subprocess.run(
            [BASH, str(SCRIPT), *args], cwd=self.directory,
            env={**self.env, **(env or {})}, text=True,
            capture_output=True, timeout=20,
        )

    def run_function(self, code, env=None):
        return subprocess.run(
            [BASH, "-c", 'source "$1"\n' + code, "test-mkcluster", str(SCRIPT)],
            cwd=self.directory, env={**self.env, **(env or {})}, text=True,
            capture_output=True, timeout=20,
        )

    def calls(self, name=None):
        calls = [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
        return [call for call in calls if name is None or call["name"] == name]

    def assert_success(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def assert_no_mutations(self):
        self.assertFalse([
            call for call in self.calls()
            if call["name"] in ("sudo", "go", "brew", "helm", "systemctl")
            or call["name"] in ("docker", "podman", "nerdctl", "kind")
            and any(arg in call["args"] for arg in ("run", "start", "create", "delete", "rm", "exec"))
        ], self.calls())

    def test_help_does_not_query_network_or_provision(self):
        result = self.run_cli("--help")
        self.assert_success(result)
        self.assertIn("--gpu", result.stdout)
        self.assertEqual(self.calls(), [])

    def test_source_has_no_external_side_effects(self):
        result = self.run_function("declare -F emit_kind_config")
        self.assert_success(result)
        self.assertIn("emit_kind_config", result.stdout)
        self.assertEqual(self.calls(), [])

    def test_valued_option_spellings_set_the_same_values(self):
        options = (
            ("-n", "--name", "cluster_name", "demo"),
            ("-i", "--ip-family", "ip_family", "dual"),
            ("-P", "--provider", "provider", "nerdctl"),
            ("-k", "--k8s-version", "kubernetes_version", "1.34"),
            ("-a", "--api-server-address", "api_server_address", "127.0.0.2"),
            ("-p", "--api-server-port", "api_server_port", "16443"),
            ("-W", "--workers", "worker_count", "2"),
            ("-w", "--wait", "readiness_timeout", "30s"),
            ("-f", "--feature-gate", "feature_gates[0]", "MemoryQoS=true"),
            (None, "--node-image", "kind_node_image", NODE_IMAGE),
            (None, "--http-port", "http_port", "8080"),
            (None, "--https-port", "https_port", "8443"),
            (None, "--registry-name", "registry_name", "demo-registry"),
            (None, "--registry-port", "registry_port", "5002"),
            (None, "--kubeconfig", "kubeconfig_path", "/a path/config"),
            (None, "--write-lock", "lock_path", "settings.lock"),
        )
        for short, long, variable, value in options:
            spellings = [(long, value), (long + "=" + value,)]
            if short:
                spellings.append((short, value))
            for arguments in spellings:
                with self.subTest(arguments=arguments):
                    result = self.run_function(
                        "parse_args " + shlex.join(arguments)
                        + '\nprintf \'%s\\n\' "${' + variable + '}"\n'
                    )
                    self.assert_success(result)
                    self.assertEqual(result.stdout, value + "\n")
        self.assertEqual(self.calls(), [])

    def test_valued_options_reject_missing_values(self):
        short_options = ("-n", "-i", "-P", "-k", "-a", "-p", "-W", "-w", "-f")
        long_options = (
            "--name", "--ip-family", "--provider", "--k8s-version",
            "--api-server-address", "--api-server-port", "--workers", "--wait",
            "--feature-gate", "--node-image", "--http-port", "--https-port",
            "--registry-name", "--registry-port", "--kubeconfig", "--write-lock",
        )
        arguments = (*short_options, *long_options, *(option + "=" for option in long_options))
        for option in arguments:
            with self.subTest(option=option):
                result = self.run_function("parse_args " + shlex.quote(option))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Missing value", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_optional_gpu_mount_missing_path_is_a_strict_mode_no_op(self):
        result = self.run_function("""
append_gpu_mount_if_present '' /ignored
append_gpu_mount_if_present "$TMPDIR/absent-library" /ignored
printf 'mount-count=%s\\n' "${#gpu_mounts[@]}"
""")
        self.assert_success(result)
        self.assertEqual(result.stdout, "mount-count=0\n")
        self.assertEqual(self.calls(), [])

    def test_help_padding_succeeds_under_strict_mode(self):
        result = self.run_function("""
printf '['
spaces 0
printf ']['
spaces 3
printf ']\\n'
""")
        self.assert_success(result)
        self.assertEqual(result.stdout, "[][   ]\n")

    def test_ansi_helpers_preserve_text_without_sed(self):
        sed = self.bin / "sed"
        sed.write_text(STUB)
        sed.chmod(0o755)
        result = self.run_function("""
colored=$'\\e[1;31malpha\\e[0m \\e[mbeta\\e[0m'
strip_ansi "$colored"
printf '\\n'
visible_length "$colored"
printf '\\n'
strip_ansi $'preserve \\e[2K non-SGR'
""")
        self.assert_success(result)
        self.assertEqual(result.stdout, "alpha beta\n10\npreserve \x1b[2K non-SGR")
        self.assertEqual(self.calls("sed"), [])

    def test_plain_and_colored_help_text_wrap_at_the_same_visible_width(self):
        sed = self.bin / "sed"
        sed.write_text(STUB)
        sed.chmod(0o755)
        result = self.run_function("""
print_wrapped_text '> ' '  ' 10 'alpha beta gamma'
print_wrapped_text '> ' '  ' 10 $'\\e[31malpha\\e[0m beta gamma'
""")
        self.assert_success(result)
        self.assertEqual(result.stdout, "> alpha beta\n  gamma\n> \x1b[31malpha\x1b[0m beta\n  gamma\n")
        self.assertEqual(self.calls("sed"), [])

    def test_latest_image_selection_drains_large_release_inventory(self):
        release = self.directory / "release.txt"
        images = [f"kindest/node:v1.35.{index}@sha256:{index:064x}" for index in range(10000)]
        release.write_text("\n".join(images) + "\n")
        result = self.run_function("""
release_json="$(<"$MKCLUSTER_TEST_RELEASE_FILE")"
detect_latest_kind_node_image "$release_json"
""", env={"MKCLUSTER_TEST_RELEASE_FILE": str(release)})
        self.assert_success(result)
        self.assertEqual(result.stdout, images[0] + "\n")
        self.assertEqual(self.calls(), [])

    def test_explicit_version_uses_selected_kind_release_inventory(self):
        # Synthetic release metadata exercises versions beyond the fallback table.
        selected_image = "kindest/node:v1.37.2@sha256:" + "d" * 64
        newer_image = "kindest/node:v1.38.0@sha256:" + "e" * 64
        release_url = "https://api.github.com/repos/kubernetes-sigs/kind/releases/tags/v0.33.0"
        self.respond("curl", contains=[release_url], stdout=json.dumps({
            "tag_name": "v0.33.0", "body": newer_image + "\n" + selected_image,
        }))
        for version in ("1.37", "1.37.2"):
            with self.subTest(version=version):
                result = self.run_cli("--dry-run", "--k8s-version", version, env={"MKCLUSTER_KIND_VERSION": "v0.33.0"})
                self.assert_success(result)
                self.assertIn(selected_image, result.stdout)
                self.assertNotIn(newer_image, result.stdout)
        self.assertEqual(len(self.calls("curl")), 2)
        self.assertTrue(all(release_url in call["args"] for call in self.calls("curl")))
        self.assert_no_mutations()

    def test_explicit_version_uses_installed_kind_release(self):
        selected_image = "kindest/node:v1.37.2@sha256:" + "d" * 64
        release_url = "https://api.github.com/repos/kubernetes-sigs/kind/releases/tags/v0.33.0"
        self.respond("kind", prefix=["version"], stdout="kind v0.33.0 go1.26.0 linux/amd64\n")
        self.respond("curl", contains=[release_url], stdout=json.dumps({"tag_name": "v0.33.0", "body": selected_image}))
        result = self.run_cli("--dry-run", "--k8s-version", "1.37")
        self.assert_success(result)
        self.assertIn(selected_image, result.stdout)
        self.assertEqual(len(self.calls("curl")), 1)
        self.assertEqual(len(self.calls("kind")), 1)
        self.assert_no_mutations()

    def test_explicit_version_keeps_known_offline_fallback(self):
        self.respond("curl", code=6)
        result = self.run_cli("--dry-run", "--k8s-version", "1.34", env={"MKCLUSTER_KIND_VERSION": "v0.33.0"})
        self.assert_success(result)
        self.assertIn("kindest/node:v1.34.8@sha256:", result.stdout)
        self.assertEqual(len(self.calls("curl")), 1)
        self.assert_no_mutations()

    def test_unknown_explicit_version_fails_when_inventory_is_unavailable(self):
        self.respond("curl", code=6)
        result = self.run_cli("--dry-run", "--k8s-version", "1.99", env={"MKCLUSTER_KIND_VERSION": "v0.33.0"})
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("kind: Cluster", result.stdout)
        self.assertEqual(len(self.calls("curl")), 1)
        self.assert_no_mutations()

    def test_explicit_digest_skips_release_metadata(self):
        selected_image = "kindest/node:v1.37.2@sha256:" + "d" * 64
        result = self.run_cli(
            "--dry-run", "--node-image", selected_image, "--k8s-version", "1.37.2",
            env={"MKCLUSTER_KIND_VERSION": "v0.33.0"},
        )
        self.assert_success(result)
        self.assertIn(selected_image, result.stdout)
        self.assertEqual(self.calls(), [])

    def test_docker_runtime_query_failure_rejects_success_looking_output(self):
        self.respond("docker", prefix=["info", "--format"], stdout="nvidia\n", code=1)
        self.respond("docker", prefix=["info"])
        result = self.run_function("""
provider=docker
container_cli=docker
gpu_detected=true
require_gpu_provider_prereqs
printf 'unexpected-success\\n'
""")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("unexpected-success", result.stdout)
        self.assert_no_mutations()

    def test_dry_run_emits_cpu_config_with_custom_ports(self):
        result = self.run_cli(
            "--dry-run", "--name", "demo", "--workers", "2",
            "--http-port", "8080", "--https-port", "8443",
            "--api-server-address", "127.0.0.1",
            env=self.pinned_env,
        )
        self.assert_success(result)
        self.assertTrue(result.stdout.startswith("kind: Cluster\n"), result.stdout)
        self.assertEqual(result.stdout.count("role: worker"), 2)
        self.assertIn("hostPort: 8080", result.stdout)
        self.assertIn("hostPort: 8443", result.stdout)
        self.assertNotIn("nvidia", result.stdout)
        self.assertNotIn("extraMounts:", result.stdout)
        self.assertEqual(self.calls(), [])

    def test_default_cluster_name_normalizes_desktop_hostname(self):
        hostname = self.bin / "hostname"
        hostname.write_text(STUB)
        hostname.chmod(0o755)
        self.respond("hostname", prefix=["-I"], stdout="127.0.0.1\n")
        self.respond("hostname", prefix=["-i"], stdout="127.0.0.1\n")
        self.respond("hostname", stdout="MacBook-Pro.local\n")
        lock = self.directory / "settings.lock"
        result = self.run_cli("--dry-run", "--write-lock", str(lock), env=self.pinned_env)
        self.assert_success(result)
        self.assertIn("MKCLUSTER_NAME=macbook-pro-local\n", lock.read_text())

    def test_invalid_values_fail_before_mutation(self):
        for args in (
            ("--provider", "invalid"), ("--workers", "-1"),
            ("--http-port", "65536"), ("--https-port", "0"),
            ("--registry-port", "abc"), ("--name", "bad/name"),
        ):
            with self.subTest(args=args):
                result = self.run_cli("--dry-run", *args)
                self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assert_no_mutations()

    def test_profile_environment_and_cli_precedence(self):
        profile = self.directory / "profile"
        profile.write_text("MKCLUSTER_WORKER_COUNT=1\nMKCLUSTER_HTTP_PORT=8081\n"
                           "MKCLUSTER_KIND_VERSION=v0.31.0\nMKCLUSTER_KIND_NODE_IMAGE=" + NODE_IMAGE + "\n")
        result = self.run_cli("--dry-run", "--profile", str(profile), env={"MKCLUSTER_WORKER_COUNT": "2"})
        self.assert_success(result)
        self.assertEqual(result.stdout.count("role: worker"), 2)
        self.assertIn("hostPort: 8081", result.stdout)
        result = self.run_cli("--dry-run", "--profile", str(profile), "--workers", "4", env={"MKCLUSTER_WORKER_COUNT": "2"})
        self.assert_success(result)
        self.assertEqual(result.stdout.count("role: worker"), 4)
        self.assertEqual(self.calls(), [])

    def test_profile_rejects_unknown_keys_without_executing_shell(self):
        profile = self.directory / "profile"
        marker = self.directory / "executed"
        profile.write_text("UNKNOWN_KEY=$(touch " + str(marker) + ")\n")
        result = self.run_cli("--dry-run", "--profile", str(profile))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(marker.exists())
        self.assertEqual(self.calls(), [])

    def test_profile_values_are_not_shell_evaluated(self):
        profile = self.directory / "profile"
        marker = self.directory / "executed"
        profile.write_text("MKCLUSTER_WORKER_COUNT=$(touch " + str(marker) + ")\n")
        result = self.run_cli("--dry-run", "--profile", str(profile))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(marker.exists())
        self.assert_no_mutations()

    def test_lock_round_trip_preserves_generated_config_without_network(self):
        lock = self.directory / "settings.lock"
        result = self.run_cli(
            "--dry-run", "--write-lock", str(lock), "--name", "locked",
            "--workers", "2", "--http-port", "8080", "--https-port", "8443",
            "--feature-gate", "MemoryQoS=true", "--kubeconfig", "/a path/config",
            env=self.pinned_env,
        )
        self.assert_success(result)
        self.assertIn("MKCLUSTER_KIND_NODE_IMAGE=" + NODE_IMAGE, lock.read_text())
        self.assertIn("MKCLUSTER_KIND_VERSION=v0.31.0", lock.read_text())
        replay = self.run_cli("--dry-run", "--profile", str(lock))
        self.assert_success(replay)
        self.assertEqual(replay.stdout, result.stdout)
        self.assertEqual(self.calls(), [])

    def test_lock_rejects_unpinned_image_without_overwriting_existing_file(self):
        lock = self.directory / "settings.lock"
        lock.write_text("original\n")
        result = self.run_cli(
            "--dry-run", "--write-lock", str(lock), "--node-image", "kindest/node:v1.35.5",
            env=self.pinned_env,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(lock.read_text(), "original\n")
        self.assert_no_mutations()

    def test_cli_kubernetes_version_overrides_profile_node_image(self):
        self.respond("curl", code=6)
        profile = self.directory / "settings.lock"
        profile.write_text(
            "MKCLUSTER_KIND_VERSION=v0.31.0\nMKCLUSTER_K8S_VERSION=1.35.5\n"
            "MKCLUSTER_KIND_NODE_IMAGE=" + NODE_IMAGE + "\n"
        )
        result = self.run_cli("--dry-run", "--profile", str(profile), "--k8s-version", "1.34")
        self.assert_success(result)
        self.assertIn("kindest/node:v1.34.8@sha256:", result.stdout)
        self.assertNotIn(NODE_IMAGE, result.stdout)
        self.assertEqual([call["name"] for call in self.calls()], ["curl"])

    def test_environment_version_overrides_profile_image(self):
        self.respond("curl", code=6)
        profile = self.directory / "settings.lock"
        profile.write_text(
            "MKCLUSTER_KIND_VERSION=v0.31.0\nMKCLUSTER_K8S_VERSION=1.35.5\n"
            "MKCLUSTER_KIND_NODE_IMAGE=" + NODE_IMAGE + "\n"
        )
        for version_variable in ("MKCLUSTER_K8S_VERSION", "MKCLUSTER_KUBERNETES_VERSION"):
            with self.subTest(version_variable=version_variable):
                result = self.run_cli("--dry-run", "--profile", str(profile), env={version_variable: "1.34"})
                self.assert_success(result)
                self.assertIn("kindest/node:v1.34.8@sha256:", result.stdout)
                self.assertNotIn(NODE_IMAGE, result.stdout)
        self.assertEqual([call["name"] for call in self.calls()], ["curl", "curl"])

    def test_environment_image_overrides_profile_version(self):
        profile = self.directory / "settings.profile"
        profile.write_text(
            "MKCLUSTER_KIND_VERSION=v0.31.0\nMKCLUSTER_K8S_VERSION=1.35.5\n"
            "MKCLUSTER_KIND_NODE_IMAGE=" + NODE_IMAGE + "\n"
        )
        lock = self.directory / "settings.lock"
        override_image = "kindest/node:v1.34.8@sha256:" + "c" * 64
        result = self.run_cli(
            "--dry-run", "--profile", str(profile), "--write-lock", str(lock),
            env={"MKCLUSTER_KIND_NODE_IMAGE": override_image},
        )
        self.assert_success(result)
        self.assertIn(override_image, result.stdout)
        self.assertNotIn(NODE_IMAGE, result.stdout)
        self.assertIn("MKCLUSTER_K8S_VERSION=1.34.8\n", lock.read_text())
        self.assertEqual(self.calls(), [])

    def test_explicit_image_and_kubernetes_version_conflict_in_either_order(self):
        for args in (
            ("--node-image", NODE_IMAGE, "--k8s-version", "1.34"),
            ("--k8s-version", "1.34", "--node-image", NODE_IMAGE),
        ):
            with self.subTest(args=args):
                result = self.run_cli("--dry-run", *args, env=self.pinned_env)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("image", result.stderr.lower())
        self.assertEqual(self.calls(), [])

    def test_matching_explicit_image_and_version_work_in_either_order(self):
        for args in (
            ("--node-image", NODE_IMAGE, "--k8s-version", "1.35.5"),
            ("--k8s-version", "1.35.5", "--node-image", NODE_IMAGE),
        ):
            with self.subTest(args=args):
                result = self.run_cli("--dry-run", *args, env=self.pinned_env)
                self.assert_success(result)
                self.assertIn(NODE_IMAGE, result.stdout)
        self.assertEqual(self.calls(), [])

    def test_cli_feature_gate_replaces_inherited_value_without_duplicate_yaml(self):
        result = self.run_cli(
            "--dry-run", "--feature-gate", "MemoryQoS=true",
            env={**self.pinned_env, "MKCLUSTER_FEATURE_GATES": "MemoryQoS=false"},
        )
        self.assert_success(result)
        self.assertEqual(result.stdout.count('"MemoryQoS":'), 1)
        self.assertIn('"MemoryQoS": true', result.stdout)
        self.assertNotIn('"MemoryQoS": false', result.stdout)

    def test_gpu_dra_enables_inherited_disabled_gates(self):
        result = self.run_function("""
initialize_defaults
parse_args --gpu-dra
validate_args
gpu_detected=true
emit_kind_config
""", env={**self.pinned_env, "MKCLUSTER_FEATURE_GATES": "DRAPartitionableDevices=false,DRAConsumableCapacity=false"})
        self.assert_success(result)
        for gate in ("DRAPartitionableDevices", "DRAConsumableCapacity"):
            self.assertEqual(result.stdout.count('"' + gate + '":'), 1)
            self.assertIn('"' + gate + '": true', result.stdout)
        self.assertEqual(self.calls(), [])

    def test_gpu_dra_rejects_explicitly_disabled_required_gates(self):
        for args in (
            ("--gpu-dra", "--feature-gate", "DRAPartitionableDevices=false"),
            ("--feature-gate", "DRAConsumableCapacity=false", "--gpu-dra"),
        ):
            with self.subTest(args=args):
                result = self.run_cli("--dry-run", *args, env=self.pinned_env)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("explicitly disabled", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_profile_rejects_duplicate_keys(self):
        profile = self.directory / "profile"
        profile.write_text("MKCLUSTER_WORKER_COUNT=1\nMKCLUSTER_WORKER_COUNT=2\n")
        result = self.run_cli("--dry-run", "--profile", str(profile))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Duplicate", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_forwarded_identity_override_is_rejected(self):
        for args in (("--name", "other"), ("-nother",), ("--config=other.yaml",), ("--image", "other")):
            with self.subTest(args=args):
                result = self.run_cli("--dry-run", "--", *args)
                self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(), [])

    def test_doctor_checks_prerequisites_without_mutation_or_download(self):
        self.respond("sysctl", stdout="1\n")
        self.respond("docker", prefix=["info"])
        result = self.run_cli("--doctor", "--provider", "docker")
        self.assert_success(result)
        self.assertIn("Prerequisite checks passed", result.stdout)
        self.assertTrue(all(call["name"] in ("sysctl", "docker") for call in self.calls()), self.calls())
        self.assert_no_mutations()

    def test_missing_prerequisite_is_reported_without_auto_install(self):
        result = self.run_function("""
command() {
  if [[ "$1" == -v && "$2" == kubectl ]]; then return 1; fi
  builtin command "$@"
}
check_deps
""")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Missing prerequisite: kubectl", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_kubeconfig_pass_through_is_normalized(self):
        result = self.run_function("""
initialize_defaults
parse_args -- --kubeconfig '/a path/config' --retain
printf '%s\\n' "$kubeconfig_path"
printf '<%s>\\n' "${kind_extra_args[@]}"
""")
        self.assert_success(result)
        self.assertIn("/a path/config\n", result.stdout)
        self.assertIn("<--retain>", result.stdout)
        self.assertNotIn("<--kubeconfig>", result.stdout)
        self.assertEqual(self.calls(), [])

    def test_forwarded_kubeconfig_overrides_environment(self):
        result = self.run_function("""
initialize_defaults
parse_args -- --kubeconfig '/cli path/config'
printf '%s\\n' "$kubeconfig_path"
""", env={"MKCLUSTER_KUBECONFIG": "/environment/config"})
        self.assert_success(result)
        self.assertEqual(result.stdout, "/cli path/config\n")
        self.assertEqual(self.calls(), [])

    def test_conflicting_explicit_kubeconfigs_are_rejected(self):
        for args in (
            ("--kubeconfig", "/first", "--kubeconfig", "/second"),
            ("--kubeconfig=/first", "--kubeconfig=/second"),
            ("--kubeconfig", "/first", "--", "--kubeconfig", "/second"),
        ):
            with self.subTest(args=args):
                result = self.run_cli("--dry-run", *args, env=self.pinned_env)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Conflicting kubeconfig", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_all_clients_target_selected_cluster(self):
        for command in ("kind", "kubectl", "helm"):
            self.respond(command)
        result = self.run_function("""
provider=podman
cluster_name=demo
kubeconfig_path='/a path/config'
kind_cmd get nodes --name "$cluster_name"
kubectl_cmd get nodes
helm_cmd list
""")
        self.assert_success(result)
        self.assertEqual(self.calls("kind")[0]["provider"], "podman")
        for command, context_flag in (("kubectl", "--context"), ("helm", "--kube-context")):
            args = self.calls(command)[0]["args"]
            self.assertEqual(args[args.index("--kubeconfig") + 1], "/a path/config")
            self.assertEqual(args[args.index(context_flag) + 1], "kind-demo")

    def test_gpu_detection_rejects_empty_inventory(self):
        self.respond("nvidia-smi", stdout="\n")
        result = self.run_function("detect_gpu_hardware")
        self.assertNotEqual(result.returncode, 0)
        self.assert_no_mutations()

    def test_gpu_detection_rejects_failed_inventory(self):
        self.respond("nvidia-smi", stdout=GPU_UUID + "\n", code=1)
        result = self.run_function("detect_gpu_hardware")
        self.assertNotEqual(result.returncode, 0)
        self.assert_no_mutations()

    def test_gpu_detection_rejects_malformed_inventory(self):
        self.respond("nvidia-smi", stdout="No devices were found\n")
        result = self.run_function("detect_gpu_hardware")
        self.assertNotEqual(result.returncode, 0)
        self.assert_no_mutations()

    def test_gpu_detection_accepts_valid_uuid_inventory(self):
        self.respond("nvidia-smi", stdout=GPU_UUID + "\n")
        result = self.run_function('detect_gpu_hardware\nprintf "detected=%s\\n" "$gpu_detected"')
        self.assert_success(result)
        self.assertIn("detected=true", result.stdout)
        self.assert_no_mutations()

    def test_gpu_cli_actions_fail_before_any_provisioning_without_gpu(self):
        self.respond("nvidia-smi", stdout="")
        for args in (("--gpu",), ("--gpu-dra",), ("--repair-gpu-mounts",), ("--dry-run", "--gpu")):
            with self.subTest(args=args):
                result = self.run_cli(*args)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("Cluster kind is ready", result.stdout)
        self.assertTrue(all(call["name"] == "nvidia-smi" for call in self.calls()), self.calls())
        self.assert_no_mutations()

    def assert_native_gpu_mounts(self, architecture, abi, node_directory):
        uname = self.bin / "uname"
        uname.write_text(STUB)
        uname.chmod(0o755)
        self.respond("uname", stdout=architecture + "\n")
        libraries = self.directory / "libraries"
        libraries.mkdir()
        native_ml = libraries / "libnvidia-ml.so.1"
        native_cuda = libraries / "libcuda.so.1"
        native_ml.write_text("native NVML")
        native_cuda.write_text("native CUDA")
        self.respond("ldconfig", stdout=(
            "libnvidia-ml.so.1 (libc6) => /compat32/libnvidia-ml.so.1\n"
            "libcuda.so.1 (libc6) => /compat32/libcuda.so.1\n"
            f"libnvidia-ml.so.1 (libc6,{abi}) => {native_ml}\n"
            f"libcuda.so.1 (libc6,{abi}) => {native_cuda}\n"
        ))
        result = self.run_function("""
gpu_detected=true
detect_gpu_mounts
printf '%s\\n' "${gpu_mounts[@]}"
""")
        self.assert_success(result)
        self.assertIn(str(native_ml) + "|" + node_directory + "/libnvidia-ml.so.1", result.stdout)
        self.assertIn(str(native_cuda) + "|" + node_directory + "/libcuda.so.1", result.stdout)
        self.assertNotIn("compat32", result.stdout)

    def test_arm64_gpu_mounts_select_native_libraries(self):
        self.assert_native_gpu_mounts("aarch64", "AArch64", "/usr/lib/aarch64-linux-gnu")

    def test_amd64_gpu_mounts_ignore_32_bit_compatibility_libraries(self):
        self.assert_native_gpu_mounts("x86_64", "x86-64", "/usr/lib/x86_64-linux-gnu")

    def test_no_nvidia_config_without_successful_detection(self):
        result = self.run_function("""
gpu_enabled=true
gpu_detected=false
emit_kind_config
""")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("nvidia.com", result.stdout)
        self.assert_no_mutations()

    def test_no_nvidia_install_without_successful_detection(self):
        result = self.run_function("""
gpu_enabled=true
gpu_detected=false
install_nvidia_dra_driver
""")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls("helm"), [])

    def configure_registry_responses(self, state):
        self.respond("docker", prefix=["ps"], stdout="kind-registry\n" if state is not None else "")
        self.respond("docker", prefix=["inspect"], contains=["{{.State.Running}}"], stdout=state or "")
        self.respond("docker", prefix=["inspect"], stdout="127.0.0.1:5001\n")
        self.respond("docker", prefix=["start"])
        self.respond("docker", prefix=["run"])

    def ensure_registry(self):
        return self.run_function("""
container_cli=docker
registry_name=kind-registry
registry_port=5001
ensure_registry
""")

    def test_stopped_registry_is_restarted(self):
        self.configure_registry_responses("false")
        self.assert_success(self.ensure_registry())
        self.assertEqual(len([call for call in self.calls("docker") if call["args"][0] == "start"]), 1)
        self.assertFalse([call for call in self.calls("docker") if call["args"][0] == "run"])

    def test_running_registry_is_reused(self):
        self.configure_registry_responses("true")
        self.assert_success(self.ensure_registry())
        self.assert_no_mutations()

    def test_missing_registry_is_created(self):
        self.configure_registry_responses(None)
        self.assert_success(self.ensure_registry())
        runs = [call for call in self.calls("docker") if call["args"][0] == "run"]
        self.assertEqual(len(runs), 1)
        self.assertIn("127.0.0.1:5001:5000", runs[0]["args"])

    def test_registry_inspection_failure_does_not_create_duplicate(self):
        self.respond("docker", prefix=["ps"], stdout="kind-registry\n")
        self.respond("docker", prefix=["inspect"], code=1)
        result = self.ensure_registry()
        self.assertNotEqual(result.returncode, 0)
        self.assert_no_mutations()

    def test_registry_port_mismatch_fails_without_replacing_container(self):
        self.respond("docker", prefix=["ps"], stdout="kind-registry\n")
        self.respond("docker", prefix=["inspect"], stdout="127.0.0.1:9999\n")
        result = self.ensure_registry()
        self.assertNotEqual(result.returncode, 0)
        self.assert_no_mutations()

    def configure_strict_port_fields(self, provider):
        correct_field, wrong_field = (".HostIP", ".HostIp") if provider == "podman" else (".HostIp", ".HostIP")
        self.respond(provider, prefix=["inspect"], fragments=[wrong_field], code=98, stderr="unsupported port field " + wrong_field)
        return correct_field

    def test_registry_reuse_uses_provider_specific_port_fields(self):
        for provider in ("docker", "podman"):
            with self.subTest(provider=provider):
                field = self.configure_strict_port_fields(provider)
                self.respond(provider, prefix=["ps"], stdout="kind-registry\n")
                self.respond(provider, prefix=["inspect"], contains=["{{.State.Running}}"], stdout="true\n")
                self.respond(provider, prefix=["inspect"], fragments=[field, '"5000/tcp"'], stdout="127.0.0.1:5001\n")
                result = self.run_function(
                    "provider=" + provider + "\ncontainer_cli=" + shlex.quote(str(self.bin / provider))
                    + "\nregistry_name=kind-registry\nregistry_port=5001\nensure_registry\n"
                )
                self.assert_success(result)
        self.assert_no_mutations()

    def test_registry_multiple_bindings_are_not_silently_accepted(self):
        self.respond("docker", prefix=["ps"], stdout="kind-registry\n")
        self.respond("docker", prefix=["inspect"], stdout="127.0.0.1:5001\n0.0.0.0:5001\n")
        result = self.ensure_registry()
        self.assertNotEqual(result.returncode, 0)
        self.assert_no_mutations()

    def inspect_resume_bindings(self, provider, family, ingress_address):
        field = self.configure_strict_port_fields(provider)
        api_address = "::1" if family == "ipv6" else "127.0.0.1"
        self.respond("kind", prefix=["get", "nodes"], stdout="demo-control-plane\ndemo-worker\n")
        self.respond(provider, prefix=["image", "inspect"], fragments=["{{.Id}}"], stdout="sha256:" + "b" * 64 + "\n")
        self.respond(provider, prefix=["inspect"], fragments=["{{.Image}}"], stdout="sha256:" + "b" * 64 + "\n")
        for port, address in ((80, ingress_address), (443, ingress_address), (6443, api_address)):
            self.respond(provider, prefix=["inspect"], fragments=[field, '"' + str(port) + '/tcp"'], stdout=f"{address}:{port}\n")
        return self.run_function("""
initialize_defaults
cluster_name=demo
worker_count=1
cluster_exists=true
resume_enabled=true
""" + "provider=" + provider + "\ncontainer_cli=" + provider + "\nip_family=" + family
            + "\napi_server_address=" + api_address + "\nvalidate_resume_cluster\n", env=self.pinned_env)

    def test_docker_and_podman_resume_use_provider_specific_port_fields(self):
        for provider in ("docker", "podman"):
            with self.subTest(provider=provider):
                self.assert_success(self.inspect_resume_bindings(provider, "ipv4", "0.0.0.0"))
        self.assert_no_mutations()

    def test_ipv6_resume_accepts_ipv6_wildcard_ports(self):
        for provider in ("docker", "podman"):
            with self.subTest(provider=provider):
                self.assert_success(self.inspect_resume_bindings(provider, "ipv6", "::"))
        self.assert_no_mutations()

    def test_ipv6_resume_rejects_ipv4_wildcard_ports(self):
        result = self.inspect_resume_bindings("docker", "ipv6", "0.0.0.0")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("expected :::80", result.stderr)
        self.assert_no_mutations()

    def test_dual_stack_resume_keeps_ipv4_wildcard_ports(self):
        self.assert_success(self.inspect_resume_bindings("docker", "dual", "0.0.0.0"))
        self.assert_no_mutations()

    def test_dual_stack_resume_rejects_ipv6_only_wildcard_ports(self):
        result = self.inspect_resume_bindings("docker", "dual", "::")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("expected 0.0.0.0:80", result.stderr)
        self.assert_no_mutations()

    def test_resume_rejects_additional_port_bindings(self):
        self.configure_strict_port_fields("docker")
        self.respond("docker", prefix=["inspect"], fragments=[".HostIp", '"80/tcp"'], stdout="0.0.0.0:80\n:::80\n")
        result = self.run_function("""
provider=docker
container_cli=docker
validate_resume_port demo-control-plane 80 0.0.0.0 80
""")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("expected 0.0.0.0:80", result.stderr)
        self.assert_no_mutations()

    def test_resume_requires_existing_cluster(self):
        self.respond("kind", prefix=["get", "clusters"], stdout="other\n")
        result = self.run_function("""
provider=docker
cluster_name=demo
resume_enabled=true
check_cluster_state
""")
        self.assertNotEqual(result.returncode, 0)
        self.assert_no_mutations()

    def test_existing_cluster_requires_explicit_resume(self):
        self.respond("kind", prefix=["get", "clusters"], stdout="demo\n")
        result = self.run_function("""
provider=docker
cluster_name=demo
resume_enabled=false
check_cluster_state
""")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--resume", result.stderr)
        self.assert_no_mutations()

    def test_resume_uses_selected_provider_and_preserves_cluster(self):
        self.respond("kind", prefix=["get", "clusters"], stdout="other\ndemo\n")
        result = self.run_function("""
provider=podman
cluster_name=demo
resume_enabled=true
check_cluster_state
printf 'exists=%s\\n' "$cluster_exists"
""")
        self.assert_success(result)
        self.assertIn("exists=true", result.stdout)
        self.assertEqual(self.calls("kind")[0]["provider"], "podman")
        self.assert_no_mutations()

    def test_gpu_resume_rejects_cpu_workers_before_registry_or_helm_mutation(self):
        self.respond("kind", prefix=["get", "nodes"], stdout="demo-control-plane\ndemo-worker\n")
        self.respond("docker", prefix=["inspect"], fragments=[".Mounts"], stdout="")
        result = self.run_function("""
initialize_defaults
cluster_name=demo
worker_count=1
cluster_exists=true
resume_enabled=true
provider=docker
container_cli=docker
gpu_enabled=true
gpu_detected=true
gpu_mounts=("$(command -v nvidia-smi)|/usr/bin/nvidia-smi")
validate_resume_cluster
ensure_registry
install_nvidia_dra_driver
""", env=self.pinned_env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("lacks the requested mount", result.stderr)
        self.assertEqual(self.calls("helm"), [])
        self.assertFalse([call for call in self.calls("docker") if call["args"][0] == "ps"])
        self.assert_no_mutations()

    def inspect_nerdctl_resume_images(self, control_plane_image, worker_image):
        self.respond("kind", prefix=["get", "nodes"], stdout="demo-control-plane\ndemo-worker\n")
        self.respond("nerdctl", prefix=["image", "inspect"], fragments=["{{.Id}}"], stdout="sha256:" + "b" * 64 + "\n")
        self.respond("nerdctl", prefix=["image", "inspect"], fragments=[".RepoDigests"], stdout="kindest/node@sha256:" + "a" * 64 + "\n")
        self.respond("nerdctl", prefix=["inspect"], contains=["demo-control-plane"], fragments=["{{.Image}}"], stdout=control_plane_image + "\n")
        self.respond("nerdctl", prefix=["inspect"], contains=["demo-worker"], fragments=["{{.Image}}"], stdout=worker_image + "\n")
        for port, address in ((80, "0.0.0.0"), (443, "0.0.0.0"), (6443, "127.0.0.1")):
            self.respond("nerdctl", prefix=["inspect"], fragments=[".HostConfig.PortBindings", '"' + str(port) + '/tcp"'], stdout=f"{address}:{port}\n")
        return self.run_function("""
initialize_defaults
cluster_name=demo
worker_count=1
cluster_exists=true
resume_enabled=true
provider=nerdctl
container_cli=nerdctl
api_server_address=127.0.0.1
validate_resume_cluster
""", env=self.pinned_env)

    def test_nerdctl_resume_accepts_image_target_digest(self):
        result = self.inspect_nerdctl_resume_images("kindest/node@sha256:" + "a" * 64, "sha256:" + "a" * 64)
        self.assert_success(result)
        digest_queries = [call for call in self.calls("nerdctl") if any("RepoDigests" in arg for arg in call["args"])]
        self.assertEqual(len(digest_queries), 1)
        self.assert_no_mutations()

    def test_nerdctl_resume_rejects_unrelated_image_digest(self):
        result = self.inspect_nerdctl_resume_images("sha256:" + "c" * 64, "sha256:" + "a" * 64)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not use the requested node image", result.stderr)
        self.assert_no_mutations()

    def test_nerdctl_resume_rejects_mutable_image_reference(self):
        result = self.inspect_nerdctl_resume_images("kindest/node:v1.35.5", "sha256:" + "a" * 64)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("mutable reference", result.stderr)
        self.assert_no_mutations()

    def test_kind_creation_propagates_provider_and_kubeconfig(self):
        self.respond("kind", prefix=["create", "cluster"], read_stdin=True)
        result = self.run_function("""
initialize_defaults
provider=nerdctl
cluster_name=demo
kubeconfig_path='/a path/config'
create_cluster
""")
        self.assert_success(result)
        call = self.calls("kind")[0]
        self.assertEqual(call["provider"], "nerdctl")
        args = call["args"]
        self.assertEqual(args[args.index("--name") + 1], "demo")
        self.assertEqual(args[args.index("--kubeconfig") + 1], "/a path/config")
        self.assertIn("--wait", args)
        self.assertIn("--retain", args)

    def test_creation_failure_preserves_resources_for_resume(self):
        self.respond("kind", prefix=["create", "cluster"], code=1, read_stdin=True)
        result = self.run_function("""
initialize_defaults
create_cluster
printf 'unexpected-success\\n'
""")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("unexpected-success", result.stdout)
        self.assertIn("--resume", result.stderr)
        self.assertEqual(len(self.calls()), 1)
        self.assertIn("--retain", self.calls("kind")[0]["args"])

    def test_nvidia_helm_install_waits_and_uses_selected_context(self):
        self.respond("helm")
        result = self.run_function("""
initialize_defaults
gpu_enabled=true
gpu_detected=true
cluster_name=demo
kubeconfig_path='/a path/config'
install_nvidia_dra_driver
""")
        self.assert_success(result)
        upgrades = [call for call in self.calls("helm") if "upgrade" in call["args"]]
        self.assertEqual(len(upgrades), 1)
        args = upgrades[0]["args"]
        self.assertIn("--wait", args)
        self.assertIn("--timeout", args)
        self.assertEqual(args[args.index("--kube-context") + 1], "kind-demo")
        self.assertEqual(args[args.index("--kubeconfig") + 1], "/a path/config")

    def test_nvidia_helm_install_failure_is_fatal(self):
        self.respond("helm", contains=["upgrade"], code=1)
        self.respond("helm")
        result = self.run_function("""
initialize_defaults
gpu_enabled=true
gpu_detected=true
install_nvidia_dra_driver
printf 'unexpected-success\\n'
""")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("unexpected-success", result.stdout)

    def test_nvidia_readiness_requires_scheduled_workers(self):
        self.respond("kubectl", contains=["rollout", "status"])
        self.respond("kubectl", contains=["get", "daemonset"], stdout="0")
        result = self.run_function("""
initialize_defaults
gpu_detected=true
verify_nvidia_ready
""")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not scheduled", result.stderr)
        self.assertFalse([call for call in self.calls("kubectl") if "resourceslices" in call["args"]])

    def test_nvidia_readiness_requires_published_gpu_devices(self):
        self.respond("kubectl", contains=["rollout", "status"])
        self.respond("kubectl", contains=["get", "daemonset"], stdout="1")
        self.respond("kubectl", contains=["get", "resourceslices"], stdout="")
        result = self.run_function("""
initialize_defaults
gpu_detected=true
readiness_timeout=1s
verify_nvidia_ready
""")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no GPU devices", result.stderr)

    def test_nvidia_readiness_accepts_rollout_and_published_devices(self):
        self.respond("kubectl", contains=["rollout", "status"])
        self.respond("kubectl", contains=["get", "daemonset"], stdout="2")
        self.respond("kubectl", contains=["get", "resourceslices"], stdout="gpu-0\n")
        result = self.run_function("""
initialize_defaults
gpu_detected=true
verify_nvidia_ready
""")
        self.assert_success(result)
        slices = [call for call in self.calls("kubectl") if "resourceslices" in call["args"]]
        self.assertEqual(len(slices), 1)
        self.assertIn("gpu.nvidia.com", " ".join(slices[0]["args"]))

    def test_node_readiness_failure_is_fatal(self):
        self.respond("kubectl", contains=["wait"], code=1)
        result = self.run_function("""
initialize_defaults
verify_cluster_ready
printf 'unexpected-success\\n'
""")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("unexpected-success", result.stdout)
        self.assertIn("--resume", result.stderr)

    def test_resume_completes_partial_setup_without_recreating_cluster(self):
        self.respond("sysctl", stdout="1\n")
        self.respond("kind", prefix=["version"], stdout="kind v0.31.0 go1.26.0 linux/arm64\n")
        self.respond("kind", prefix=["get", "clusters"], stdout="demo\n")
        self.respond("kind", prefix=["export", "kubeconfig"])
        self.respond("kind", prefix=["get", "nodes"], stdout="demo-control-plane\ndemo-worker\n")
        self.respond("docker", prefix=["info"])
        self.respond("docker", prefix=["image", "inspect"], stdout="sha256:" + "b" * 64 + "\n")
        self.respond("docker", prefix=["inspect"], fragments=["{{.Image}}"], stdout="sha256:" + "b" * 64 + "\n")
        for port, address in ((80, "0.0.0.0"), (443, "0.0.0.0"), (6443, "127.0.0.1")):
            self.respond("docker", prefix=["inspect"], fragments=[".HostConfig.PortBindings", '"' + str(port) + '/tcp"'], stdout=f"{address}:{port}\n")
        self.respond("docker", prefix=["inspect"], contains=["demo-control-plane"], fragments=[".NetworkSettings.Networks"], stdout="kind\n")
        self.respond("docker", prefix=["inspect"], contains=['-f={{json (index .NetworkSettings.Networks "kind")}}'], stdout="null\n")
        self.configure_registry_responses("false")
        self.respond("docker", prefix=["network", "connect"])
        self.respond("docker", prefix=["exec"])
        self.respond("kubectl")
        result = self.run_cli(
            "--resume", "--name", "demo", "--workers", "1", "--kubeconfig", "/a path/config",
            "--api-server-address", "127.0.0.1", env=self.pinned_env,
        )
        self.assert_success(result)
        self.assertIn("Cluster demo is ready", result.stdout)
        self.assertFalse([call for call in self.calls("kind") if "create" in call["args"]])
        self.assertTrue([call for call in self.calls("docker") if call["args"][0] == "start"])
        self.assertTrue([call for call in self.calls("kubectl") if "wait" in call["args"]])
        for call in self.calls("kubectl"):
            args = call["args"]
            self.assertEqual(args[args.index("--kubeconfig") + 1], "/a path/config")
            self.assertEqual(args[args.index("--context") + 1], "kind-demo")


if __name__ == "__main__":
    unittest.main()
