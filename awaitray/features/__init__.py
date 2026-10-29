"""Features: each one owns its state, its menu actions and its background
work, and talks to the rest of the app only through a `Context`.

The controller is the composition root: it builds the features, gives each a
Context, and calls them where the PR poll needs them. A feature never
imports another feature; what one needs from another (open a link, refetch
a PR's details) comes as a callable in its Context.
"""
