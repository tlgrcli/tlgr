# Processors

Registry-based text modification functions for the Gateway pipeline (formerly
called "transforms").

## How it works

```mermaid
flowchart LR
    YAML["YAML processors list"] --> CREATE["create_chain_from_list()"]
    CREATE --> CHAIN["ProcessorChain"]
    CHAIN --> APPLY["chain.apply(text)"]
    APPLY --> OUT["modified text"]
```

Every processor is a plain function registered with `@register_processor`.
Processors take `(text: str, config: dict)` and return the modified text.

`ProcessorChain` runs multiple processors in sequence, passing the output of
one as the input to the next.

## Built-in processors

### Text processors (`text.py`)

| Processor | Description | Config keys |
|-----------|-------------|-------------|
| `replace_mentions` | Replace @mentions with replacement text | `replacement`, `pattern` |
| `remove_links` | Remove URLs | `replacement` |
| `remove_hashtags` | Remove #hashtags | `replacement` |
| `strip_formatting` | Normalize whitespace, collapse blank lines | -- |
| `add_prefix` | Prepend text (adds newline) | `prefix` |
| `add_suffix` | Append text (adds newline) | `suffix` |

### Regex processor (`regex.py`)

| Processor | Description | Config keys |
|-----------|-------------|-------------|
| `regex_replace` | Custom regex substitution | `pattern`, `replacement`, `flags` |

Flags: `i` (case-insensitive), `m` (multiline), `s` (dotall).

## Configuration

Processors can be specified at two levels:

### Job-level

Applied to all actions (unless an action overrides):

```yaml
jobs:
  - name: my-job
    processors:
      - strip_formatting
      - add_prefix:prefix=[NEWS]
    actions:
      - forward:
          to: ["@dest"]
```

### Per-action level

Overrides job-level processors for this action only:

```yaml
actions:
  - forward:
      to: ["@dest"]
      processors:
        - strip_formatting
        - type: regex
          pattern: "sponsor"
          replacement: ""
          flags: i
```

### Config formats

Processors in YAML can be:

1. **String name**: `"strip_formatting"`
2. **String with inline config**: `"add_prefix:prefix=[NEWS]"`
3. **Inline regex dict**:
   ```yaml
   - type: regex
     pattern: "\\bfoo\\b"
     replacement: "bar"
     flags: i
   ```

## Adding a custom processor

1. Create a function that takes `(text: str, config: dict)` and returns `str`.
2. Decorate it with `@register_processor("name")`.
3. Import it in `__init__.py`.

```python
# tlgr/processors/my_proc.py
from tlgr.processors import register_processor


@register_processor("uppercase")
def uppercase(text, config=None):
    return text.upper()
```

Then import in `__init__.py`:

```python
from tlgr.processors import my_proc  # noqa: F401
```

Now usable in YAML:

```yaml
processors:
  - uppercase
```
