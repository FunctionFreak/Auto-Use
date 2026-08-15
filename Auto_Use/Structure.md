# Structure Notes

- `windows`, `mac`, `ios` should remain structurally similar. This makes it easier to adapt features across platforms.
- Do not import code from `windows` into `mac`, or from `mac` into `windows`.
- Release binaries must remain platform-specific and separate. Importing cross-platform code into them can create build and runtime problems.
- No code mixing between platform-specific implementations should be allowed.

- Tool definitions should remain in the llm_manager.py
- Keep this behavior as it is.

- Do not add any provider that does not support web functionality for build or tool features.
- Only add models that either have built-in web capability or can support web access through a provider integration.
