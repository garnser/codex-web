`conversation_channels_v3.py` is the exact, unmodified file from
`106a2daadf8c61ff0b711fa0b537e2b7a5d8d022:codex_web/conversation_channels.py`.
SHA-256: `bb50602667127c83deccec803b05014285f74d20810dc115b828583f522fba32`.
It qualifies the existing 3.0 reader against additive scalar routing metadata.

`execution_profiles_v1.py` and `execution_profile_seed_v1.py` are exact unmodified
files from `0e40f228e12b1704942c29e6c33a61a9f2be721b` at their original
`codex_web/` paths. `execution_profile_seed_v1.json` is the original seed's
output executed with that original reader. The qualification pins all SHA-256
values and reruns the original seed without replacing current application
modules. It proves 1.0 payload/checksum preservation and old-reader rejection of
an actually published canonical Project 1.1 payload.
