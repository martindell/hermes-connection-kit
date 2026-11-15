# Deployment reference — fill privately before using skill

Owner/repository: [OWNER/REPO]
Hermes host/user: [HOST / USER]
Verified on-demand CLI absolute path: [PATH_TO_ON_DEMAND_PY]
Owner-only settings absolute path: [PATH_TO_CONFIG_JSON]
Status command on Hermes host: `python3 -I [PATH_TO_ON_DEMAND_PY] --settings [PATH_TO_CONFIG_JSON] status`
Connect command on Hermes host: `python3 -I [PATH_TO_ON_DEMAND_PY] --settings [PATH_TO_CONFIG_JSON] connect`
Disconnect command on Hermes host: `python3 -I [PATH_TO_ON_DEMAND_PY] --settings [PATH_TO_CONFIG_JSON] disconnect`
Installed fragment and control port: [VERIFIED_UNIT / PORT]
Project key / issue / permanent PR / actor / owning conversation: [VERIFY_EACH]
Source commit and installed script/YAML hashes: [REVIEWED_VALUES]
Known accepted run receipts, registration and callback ownership: [ISSUE_REFERENCE]

Never store secrets, private key paths with embedded credentials, snapshots or household data in a distributed skill. Validate commands and current installed state before recommending a connection change. A saved reference is discovery, not permission to run it. Do not substitute a fictional path when actual command is unknown.
