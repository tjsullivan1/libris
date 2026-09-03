# The MCP surface is frontmatter only; note bodies stay in Obsidian

A Book Note is frontmatter plus a body, and the body is where a reader's own writing lives
(ADR 0009). The MCP tools reach only the frontmatter: they move a Book through the reading
cycle and set its fields, and they neither read nor write a body. "What did I make of
Oathbringer?" is not a question this Library answers to an agent, by choice rather than by
omission.

Reading was refused first. Handing a language model a person's private reading notes to
summarise is a different product from tracking a reading list, and ADR 0007 keeps the tool
definitions identical across stdio and Streamable HTTP - so a tool that reads bodies reads
them off the machine too, the moment the remote transport lands.

Writing went with it, though it looked separable. Appending a dictated remark means finding
where `## Notes` ends and the description callout begins, which is reading the body; and an
append the tool cannot read back is an append nobody can verify, into the one part of a note
that cannot be regenerated. Every write path here rewrites the whole file rather than
appending to it - `ensure_frontmatter_fields` re-dumps the YAML and reflows the body on every
pass - so the risk is not deletion but a round-trip bug, in a structure the Obsidian Linter
already damages four different ways. Under this decision an MCP write never runs that
round-trip at all.

The price is that "I finished it, and it dragged in the middle" loses its second clause. The
rule is worth more than the clause: *MCP moves books through the reading cycle; Obsidian holds
what you thought about them* is a boundary that answers the next tool's scope question without
being re-argued. Adding body writes later is easy; withdrawing them once an agent relies on
them is not.
