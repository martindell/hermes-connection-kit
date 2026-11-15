# Fresh host (not a migration)

These are ordered operator steps, not an installer or authority to change a shared host. Resolve the external tunnel client, Hermes gateway, MCP registration and Work webhook capability in [README](../README.md) first. For an existing installation use [MIGRATION.md](../MIGRATION.md). Stop on unexpected files, units or routes. No fictional setting is deployable.

1. Check Linux Python 3, systemd user manager, `LoadCredential=` (247+), `/proc`, user lingering, local ports, `gh` identity and exact private PR access. Verify the independently supplied tunnel executable and its YAML schema against the [fictional example](../client-config.example.yaml); confirm gateway `/v1/runs` and the tunnel's `/readyz` are different listeners. Do not enable lingering or touch a running service as a side effect.

2. Choose owner-controlled paths under an inspected private home; this example uses `$HOME/.local/lib/hermes-mcp-connection` as install_dir, `$HOME/.local/state/hermes-mcp-connection` as state_dir, `$HOME/.local/share/hermes-mcp-source` as private source directory, and `$HOME/.config/hermes-mcp-connection/config.json` for settings. On another layout, create EVERY missing ancestor of both configured directories and the unit fragment directory before setup. `secure_existing_parents` requires ancestors already present; setup's `mkdir` does not use `parents=True`. This preparation script creates only missing directories, in parent-first order. It refuses links/non-directories and checks owner/mode on existing directories instead of chmodding shared ones; run it only after reviewing the chosen paths and before putting secrets there. In particular the existing or new `$HOME/.config/systemd/user` target must be owner-only 0700. Change the example paths and review the corresponding parent list if you choose a different layout.

```sh
set -eu
for d in "$HOME/.config" "$HOME/.config/systemd" "$HOME/.config/systemd/user" \
         "$HOME/.config/hermes-mcp-connection" "$HOME/.local" "$HOME/.local/lib" \
         "$HOME/.local/state" "$HOME/.local/share" "$HOME/.local/share/hermes-mcp-source"; do
    if [ -L "$d" ] || { [ -e "$d" ] && [ ! -d "$d" ]; }; then
        printf 'Refuse non-directory or symlink: %s\n' "$d" >&2; exit 1
    fi
    if [ ! -e "$d" ]; then mkdir -m 700 "$d"; fi
    mode=$(stat -c %a "$d")
    if [ "$(stat -c %u "$d")" != "$(id -u)" ] || [ "$(( 0$mode & 022 ))" -ne 0 ]; then
        printf 'Refuse unsafe owner/writable directory (inspect, do not chmod): %s\n' "$d" >&2; exit 1
    fi
    case "$d" in
        "$HOME/.config/systemd/user"|"$HOME/.config/hermes-mcp-connection"|"$HOME/.local/share/hermes-mcp-source")
            if [ "$mode" != 700 ]; then printf 'Refuse nonprivate target: %s\n' "$d" >&2; exit 1; fi ;;
    esac
done
```

Check `$HOME` and all higher ancestors for symlinks or shared-writable permissions too; the loader enforces this. The install_dir and state_dir themselves may be absent: setup creates them 0700 after their parents exist. Do not create/change an existing managed unit or overwrite a live install. Keep the verified source scripts `on-demand.py` and `watchdog.py` in the private source directory; `install-observer` reads the sibling `watchdog.py` and `watchdog-config.json` beside the invoked on-demand script. Copy only reviewed source bytes; source dir and config must remain owner-only (0700/0600). The private checkout can serve as that source directory only if its path passes the same checks.

3. Decide whether to include the optional observer NOW, before creating/pinning the tunnel YAML. If included, choose the exact project→private repo/PR, GitHub publishing/ack actor, gateway credential file under state_dir, managed unit name/fragment, and a distinct loopback control port. Make private `watchdog-config.json` from [fictional example](../watchdog-config.example.json) beside the source scripts with mode 0600. Verify the actual `gh api` identity can access/comment on the PR under the intended service user. Set the client YAML's `hermes_register` and `hermes_tracked` origins to that same control port and its gateway start/status/stop origins to the actual gateway port; set the tunnel `/readyz` listener to the selected listener port. If observer is omitted, omit or disable those two targets in the real YAML; do not advertise dead targets. Resolve the PR route and all port/endpoint details before the next step.

4. Finalise and review the real client YAML (0600, no symlinks) and trusted executable at owner-controlled absolute paths. The YAML `env:` references are supplied by the managed process; do not put either API key in YAML. Hash executable and COMPLETE YAML independently with `sha256sum /absolute/client /absolute/client.yaml`. Any YAML change after this point needs a new review and pin BEFORE setup, not an in-place edit. Fill [config.example.json](../config.example.json) at the owner-only settings path with real absolute paths/hashes, tunnel ID, listener/gateway ports, chosen install_dir/state_dir and unit_name; the `/home/example` values and fictional hashes are not deployable. Check ownership/mode 0600, file link count, safe ancestors, distinct ports and absence of unit collision. Keep this settings path stable for every action. Do not put credentials in a repository, argv or logs.

5. From the verified private source directory, run `python3 -I "$SOURCE_DIR/on-demand.py" --settings "$HOME/.config/hermes-mcp-connection/config.json" setup` in a private interactive terminal, with SOURCE_DIR set to that directory. The `--settings` pair precedes the action. Setup prompts for the **OpenAI tunnel API key** (restricted to Tunnels Read/Use), then the **Hermes gateway API key** (the host's `API_SERVER_KEY` value, without the `Bearer ` prefix). See [the two API keys](../README.md#the-two-api-keys) for the naming and older prompt equivalents. It stores plaintext owner-only credential files under state_dir, copies reviewed scripts/YAML, writes the static tunnel unit and reloads the user manager, but does not start it. Same-UID processes can read retained keys. If observer was selected in step 3, while still stopped run `python3 -I "$SOURCE_DIR/on-demand.py" --settings "$HOME/.config/hermes-mcp-connection/config.json" install-observer`; this prepares static control/timer units, not activation. Do not edit/re-pin YAML after setup or use setup to overwrite changed installed bytes: existing installs require the stopped migration plan.

6. Separately configure the actual MCP registration and persistent Work webhook with [the template](../templates/work-webhook.md), binding the correct owning conversation and PR comment events only. Neither setup command configures Work. Keep source/accepted ID/registration receipts in [the issue ledger](../templates/project-manifest.md); install alone is not a callback.

7. Only with separate authority to activate, use the same CLI/settings path and `connect`, then `status` and [qualification](qualification.md). Connect checks linger, pins, port ownership and managed tunnel `/readyz`; with observer installed it starts control after tunnel readiness and then timer. Status alone is not interoperability proof. `disconnect` stops this tunnel/observer and notifications for ALL attached conversations, not the gateway or accepted jobs; coordinate shared use. It retains credentials/state. `uninstall` is separate and not routine disconnect.

## Practical notes from the Ubuntu VM walkthrough

The 12 November 2026 walkthrough used Ubuntu Server 24.04.3 (updated),
2 vCPUs, 4 GB RAM and a 25 GB virtual disk, with no host shares or device
passthrough. DHCP was sufficient. With Ubuntu's LVM default, “use whole disk”
can still leave free space inside the volume group: expand the root logical
volume during installation if you want all remaining space available to `/`.
Install OpenSSH for the operator terminal. Keep VM autostart off for this test.

Clone the intended branch explicitly (`git clone --branch main REPOSITORY_URL`)
and record `git rev-parse HEAD`; a repository's default branch may still be an
older development branch. Run the offline tests before installing. Record the
kit revision, gateway revision, client version and executable hash separately.

### External prerequisites are separate products

The tested gateway was [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)
at revision `2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19` (reported version 0.23.0).
Follow that revision's upstream installation instructions and record the actual
installed SHA. Authenticate the intended model provider separately; a working
local `/health` response does not prove model access. The walkthrough selected
`openai-codex` with `gpt-6.1-sol`; another operator must select a provider/model
they are authorised to use and inspect any fallback or auxiliary-provider
configuration before submitting work. The kit does not install or authenticate
Hermes Agent.

The tested tunnel distribution was the **full Linux amd64** asset from
[OpenAI tunnel-client v0.0.20](https://github.com/openai/tunnel-client/releases/tag/v0.0.20):

| Item | SHA256 |
|---|---|
| `tunnel-client-v0.0.20-linux-amd64.zip` | `2f06ce5a17425c74a827e7038692936e973ae388e7eec728c40efee1fb5f139d` |
| Extracted `tunnel-client` executable | `447e237f2030a0f0dab908b0a2f61fafce878d7da341f741e79245051319a09e` |

These hashes describe that release/architecture only. Verify the archive against
upstream release metadata before extraction; put the **executable** hash in
`client_sha256`. Python can extract it without installing unzip:

```bash
python3 -m zipfile -e tunnel-client-v0.0.20-linux-amd64.zip unpacked
chmod 700 unpacked/tunnel-client
./unpacked/tunnel-client --version
sha256sum unpacked/tunnel-client
```

Create a distinct tunnel and MCP plugin for a disposable test. Check the tunnel
ID before pinning YAML; do not reuse an active host's ID or copy its credentials.
Use the **OpenAI tunnel API key** only for the tunnel client and the **Hermes
gateway API key** only for local gateway authentication. Neither is the model
provider login or GitHub authentication.

### Handling a directory preparation refusal

On the test VM, existing `.config` and `.local/state` were group-writable. The
preparation script correctly stopped. Inspect the exact refused directory:

```bash
stat -c 'mode=%a owner=%U group=%G path=%n' "$HOME" "$HOME/.config" "$HOME/.local/state"
id
```

For an exclusively owned disposable account, the operator may explicitly choose
`chmod 700` on the individual refused directory after inspection, then rerun
preparation. Do not use recursive chmod or change shared directories blindly.
This preparation policy is deliberately stricter about group-write permission
than the runtime's private-group ancestor validation.

### Validate YAML without starting a second tunnel

With the example private paths and the verified client installed at the path
below, import into a separate temporary profile directory:

```bash
(
set -eu
umask 077
check_dir=$(mktemp -d "$HOME/.config/hermes-mcp-connection/validation.XXXXXX")
"$HOME/tunnel-client-v0.0.20/unpacked/tunnel-client" \
  --profile-dir "$check_dir" profiles add clean-test \
  --from-file "$HOME/.config/hermes-mcp-connection/client.yaml"
)
```

Adjust only the executable path to your verified location. Successful import
checks client compatibility; it does not prove live MCP delivery. The temporary
profile is not the managed service's configuration and must not be started as
an additional client.

### Make the gateway independent of SSH

Configure the gateway API on loopback (`API_SERVER_HOST=127.0.0.1`,
`API_SERVER_PORT=8642`, `API_SERVER_ENABLED=true`) with a freshly generated
`API_SERVER_KEY` in its private configuration. Preserve existing configuration;
do not overwrite an existing key or print the full environment file. Enter the
same key at kit setup's Hermes gateway API key prompt, without `Bearer `.

A foreground `hermes gateway` can stay on “Starting” while already serving.
Check from a second terminal:

```bash
curl --max-time 5 -fsS http://127.0.0.1:8642/health
```

Inspect `hermes gateway install --help` for the installed revision. The tested
revision supports an explicitly manual-start user service:

```bash
hermes gateway install --no-start-now --no-start-on-login
```

If a foreground gateway is running, stop that process with Ctrl+C and wait for
the prompt before starting the service. Do this only on the disposable host:

```bash
hermes gateway start
hermes gateway status
curl --retry 5 --retry-connrefused --retry-delay 2 --max-time 5 \
  -fsS http://127.0.0.1:8642/health
```

Confirm user lingering (`loginctl show-user "$USER" -p Linger`). If absent,
enabling it is an explicit operator step (`sudo loginctl enable-linger "$USER"`).
Linger keeps a started user service alive after logout; it does not itself enable
these disabled services at boot. Close all SSH sessions, reconnect, and repeat
gateway health and kit status checks. The gateway and tunnel have separate
lifecycles: kit disconnect does not stop the gateway or cancel jobs.

### Checkpoints and reboot recovery

Take powered-off VM snapshots after the updated OS, authenticated gateway, and
verified connection stages. Snapshots after authentication contain credentials;
keep them private. A created snapshot is not a tested restore.

After completing and acknowledging the disposable job, test kit disconnect and
reconnect, verifying retained registration and no duplicate event. Then reboot
the disposable VM. Before starting anything, inspect:

```bash
systemctl --user is-active hermes-gateway.service hermes-mcp-connection.service
/usr/bin/python3 -I "$HOME/.local/share/hermes-mcp-source/on-demand.py" \
  --settings "$HOME/.config/hermes-mcp-connection/config.json" status
```

For these manual-start service names expect inactive/disconnected (`is-active`
returns nonzero for inactive units). Start the gateway, verify health, then run
kit `connect` with the same script/settings paths. No key re-entry should be
needed. Check the exact saved registration and GitHub comments again; do not
start a replacement job just to check retained state. Adapt unit names if your
settings differ. Keep restore and overdue/pause tests as separate bounded tests.
