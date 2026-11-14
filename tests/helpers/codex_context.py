"""Inspect source parts from the actual additionalContext wire in assertions."""
def context_source(context, source):
    parts = []
    for entry in context.values():
        header, _, text = entry['value'].partition('\n')
        if f'; source {source}; part ' in header:
            parts.append((int(header.rsplit('part ', 1)[1].split('/')[0]), text))
    return ''.join(text for _, text in sorted(parts))
