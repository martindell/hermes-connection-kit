# Run the Hermes deadline watchdog on a Linux host

An always-on VM, Raspberry Pi or Linux NAS can run the watchdog as an ordinary user. Every minute it reads one GitHub issue, reconstructs who owes the next update and sends an alert if a deadline passes. A separate Work/Hermes workflow writes those handovers to the issue. A new installation watches only tasks explicitly enrolled with its session ID. It cannot detect an unrecorded promise or send an alert while its own host is down.

## What to adapt

| Setting | Choose for your installation |
| --- | --- |
| Host | Linux with Python 3.11+, a persistent local filesystem and direct HTTPS access to api.github.com |
| Journal | A dedicated issue in your own private GitHub.com repository, the account allowed to append records and a lowercase project identifier such as `demo-team` |
| Schedule | A user crontab, systemd timer or NAS scheduler running the same command once a minute |
| Destination | An executable receiving the subject and message as two arguments. The supplied example writes to local syslog. Use a mail or messaging wrapper to reach a person. |

## Install

From the repository root or extracted kit, run the commands below as the account that will run the schedule. The kit needs three adjacent Python files and uses only the standard library.

```sh
umask 077
code="$HOME/.local/lib/hermes-watchdog"
state="$HOME/.local/state/hermes-watchdog/demo-team"
install -d -m 700 "$code" "$state"
install -m 600 baton.py baton_notify.py baton_runner.py "$code/"
install -m 700 examples/notify-local-log "$code/notify-local-log"
```

For a fresh installation, edit the following four values before creating the file. Use the author's GitHub login exactly as it appears on issue comments. Each independent project gets its own state directory.

```sh
cat > "$state/config.json" <<'JSON'
{"repo":"YOUR-ACCOUNT/YOUR-REPO","issue":1,"project":"demo-team","author":"YOUR-GITHUB-LOGIN"}
JSON
```

Place a GitHub token for the selected repository, with **Issues: read** access, in `$state/github-token`. One way is to run the following in Bash. Paste the token at the hidden prompt. Keep the state directory and its contents private to the scheduler account.

```bash
read -rsp 'GitHub read token: ' journal_token && printf '\n'
printf '%s\n' "$journal_token" > "$state/github-token"
unset journal_token
chmod 600 "$state/config.json" "$state/github-token"
```

Initialise once and save the printed session ID for the journal writers. A tick without `--notify` reads the issue and saves state without submitting alerts. Expect `read_ok: true` and an empty incident list for a new session.

```sh
python3 -I "$code/baton_runner.py" --directory "$state" init
python3 -I "$code/baton_runner.py" --directory "$state" tick
```

## Choose and test the alert destination

```sh
export HERMES_BATON_NOTIFY="$code/notify-local-log"
"$HERMES_BATON_NOTIFY" 'Watchdog installation test' 'Check the local log for this message.'
```

On a systemd machine, look in `journalctl -t hermes-watchdog`. Elsewhere, check where the host routes local syslog messages. To deliver mail on a machine with a working `mail` command and configured mail transport, save this as an executable owned by the scheduler account, substitute your address and point `HERMES_BATON_NOTIFY` to its absolute path.

```sh
#!/bin/sh
set -eu
printf '%s\n' "$2" | mail -s "$1" operator@example.org
```

Test the selected hook directly and confirm receipt at the destination. The runner invokes it without a shell and allows 15 seconds. Exit zero means submission succeeded. An error or timeout allows at most three attempts, at least 15 minutes apart. A successful submission suppresses repeat alerts for that incident. The same hook receives journal and local monitor faults.

## Schedule

In the scheduler account's `crontab -e`, adapt these absolute paths, including the Python path from `command -v python3`. Cron needs its own hook setting. The example discards routine JSON output after the manual check. Keep cron error reporting configured for launch failures.

```cron
HERMES_BATON_NOTIFY=/home/operator/.local/lib/hermes-watchdog/notify-local-log
* * * * * /usr/bin/python3 -I /home/operator/.local/lib/hermes-watchdog/baton_runner.py --directory /home/operator/.local/state/hermes-watchdog/demo-team tick --notify >/dev/null
```

For another scheduler, use the same executable, arguments, environment and user. Overlapping ticks skip when another process holds the state lock. On Unraid, omitting the hook selects the built-in notification command.

## Prove the whole route

1. Run `python3 -I "$code/baton_runner.py" --directory "$state" status` twice a little over a minute apart. Verify `last_ok` advances and `fault` is false.
2. Using the configured GitHub account, post the following two-line comment on the journal issue. Replace `SESSION` with the init output and replace the numeric deadline with a **future Unix timestamp in seconds**, e.g. the output of `date -d '+3 minutes' +%s` on Linux. Supply a real reference for the test in `source`. Use fresh IDs and a fresh task name if you repeat the test.

```text
<!-- hermes-baton:v1 -->
{"session":"SESSION","project":"demo-team","task":"smoke-test","id":"smoke-open","seq":1,"actor":"work","action":"open","deadline":1900000000,"source":"operator smoke test"}
```

3. Let the deadline pass. Confirm one overdue alert at your destination. After another scheduled tick, verify `last_ok` advanced and the matching entry in the private `alerts.json` still shows `submitted` with one attempt.
4. Report the test result to the operator, then append a second comment using the same session. Replace the result and update references with your own. After the next tick, `status` should show the task at sequence 2, phase `finished`, and `fault` false.

```text
<!-- hermes-baton:v1 -->
{"session":"SESSION","project":"demo-team","task":"smoke-test","id":"smoke-finish","seq":2,"actor":"work","action":"finish","result":"operator verified one alert","user_update":"operator received test outcome"}
```

For routine handovers the sender writes an offer and the receiver accepts it. Work records completion after reviewing the result and informing the operator. Writers append each event as a new comment with the next sequence number. GitHub supplies its timestamp. Editing or deleting accepted records causes a monitoring fault. Use the configured GitHub account to append records. A shared account authenticates the writer account only. The role in the JSON is claimed by the writer.

| Action | When to write it | Extra fields |
| --- | --- | --- |
| `open` | Work enrols a task before promising an update | `source`, `deadline` |
| `offer` | The current owner hands the task to the other role | `to`, `accept_by`, `next_deadline`, `evidence` |
| `accept` | The recipient accepts responsibility for the exact offer | `offer_id`, `evidence` |
| `update` | The active owner reports progress and gives a new deadline | `deadline`, `evidence` |
| `wait_user` / `resume` | Work asks the operator a question, then records the answer | `evidence` on both, plus `deadline` on resume |
| `finish` | Work has reviewed the result and informed the operator | `result`, `user_update` |
| `cancel` | Work records authority to close the obligation | `evidence` |

An offer leaves the sender responsible until acceptance. Set the acceptance deadline before the recipient’s next update deadline. After a doorbell callback, Work accepts the returned task for review before recording a finish. Configure the writers separately with the private journal, session and project. Keep credentials and job output out of comments.

## Operating the monitor

If `last_ok` stalls, check the scheduler, executable paths and account permissions. If `fault` is true, inspect journal access and recent records. If an alert is missing, check the hook directly and inspect `alerts.json`. Each tick prints a JSON summary when run manually. Back up the private state, including the session and alert receipts. To stop monitoring, remove the schedule entry. Reuse the existing state when restarting.

To distribute the portable files yourself from a reviewed checkout:

```sh
git archive --format=tar.gz --prefix=hermes-watchdog/ -o hermes-watchdog.tar.gz HEAD \
  LICENSE baton.py baton_runner.py baton_notify.py examples/notify-local-log docs/portable-watchdog.md
```
