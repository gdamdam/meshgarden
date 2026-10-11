# Later: self-service privacy controls

Planned work, not yet built.

## Visitor commands

- [ ] `PRIVACY`: a compact explanation of what is kept and for how long (publication, mail,
  observation, update-cursor, reply-route and usefulness-metric retention).
- [ ] `MYDATA`: an authenticated summary of a visitor's stored posts and preferences.
- [ ] `FORGET`: an expiring confirmation token, authenticated ownership checks, and removal of
  eligible posts, mail, reply routes, LAB preferences, update cursors and attributable product
  measurements.

## Documentation

- [ ] Explain which passive RF observations can be collected again after a node transmits, and
  what remains in expiring backups.

## Testing

- [ ] Real-radio acceptance for the new community commands: two people, both transports, weak
  links, lost ACKs, restarts, 120-byte replies and quotas.
