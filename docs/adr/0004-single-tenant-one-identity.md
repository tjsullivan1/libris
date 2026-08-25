# Libris is single-tenant: one identity, no ownership model

Libris serves one person. There is no owner column on a Book, no per-record authorization,
and no user table. Authorization is a single question: is this Tim.

We separated two things that sound alike. *Publishing* — showing someone a read-only view of
the Library — is a static render of data we already hold, and can be added at any time
without touching the model. *Multi-user* — other people holding their own Libraries, or
writing to this one — would put an owner on every record and an authorization check on every
endpoint. Only the second is expensive to retrofit, and only the second is being declined.

Microsoft Entra is the identity plane, since the service is hosted in Azure anyway. Sign-in
uses a Gmail address via Google federation, which Entra External ID supports for Gmail
accounts specifically. Should sharing ever mean real multi-user access, this decision is the
one to revisit first.
