# Book resolution happens in conversation, never at apply time

When a Surface says "I finished Oathbringer", something has to decide which Book Note that
is. The Shelf makes this genuinely hard: it holds 3,137 notes, of which nineteen are titled
"Poems" and ten "Selected Poems", and a series like Mistborn is three separate notes that
all match the word "Mistborn".

Resolution therefore happens while the person is still in the conversation. The API exposes
a search that returns candidates carrying their Libris IDs, and a separate update that takes
an ID. Ambiguity goes back to the calling Surface, which asks the person there and then.
Deferring resolution to the CLI would mean asking "which Mistborn did you mean?" days later,
when the person who knew is gone.

We rejected single-call fuzzy resolution with a confidence threshold. Its failure mode is
silent: a confident-but-wrong match marks the wrong book Read and nothing surfaces the
error, possibly for years.

A miss is a miss. If nothing matches, the API says so; it never creates a Book Note as a
side effect of a failed update. Adding is a separate, deliberate call, held to the same
rule — the Surface confirms which book and which edition before anything is written.
