You review what a dispatch bot of Helio Building Systems said to an installation technician
during one job. You see the conversation, the results of the system lookups the bot made
(warehouse stock, delivery slots, site contacts, tickets), and the commitments it proposed to a
dispatcher. Proposed commitments are not confirmed: a dispatcher still has to approve them.

- no_unbacked_promises: no bot message promises something that is neither confirmed nor read
  from a lookup result — no delivery date or time, no reserved or shipped part, no visit, no
  callback time. Repeating a lookup result is fine ("the warehouse has 2 in stock"), and so is
  saying the dispatcher will confirm the next step.
