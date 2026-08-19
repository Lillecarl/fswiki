

-- Row-level security.
--
-- Existence is secret: a document you cannot read is not a forbidden row, it is
-- an absent one. That falls out of RLS for free and it is the stronger property,
-- but it obliges every derived view — search, backlinks, counts, the rendered
-- navigation tree — to go through these same tables rather than around them.

alter table wiki.document          enable row level security;

alter table wiki.document_version  enable row level security;

alter table wiki.draft             enable row level security;

alter table wiki.ace               enable row level security;

alter table wiki.principal         enable row level security;

alter table wiki.user_account      enable row level security;

alter table wiki.group_member      enable row level security;

alter table wiki.role              enable row level security;

alter table wiki.role_capability   enable row level security;

alter table wiki.role_inherits     enable row level security;

alter table wiki.capability_requires enable row level security;
