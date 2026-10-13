# Documentation homepage

Audience: frum families first. Version 1 is a useful family computer; professional daily use is a direction, not a universal compatibility claim.

## Visual system

Reuse `docs/stylesheets/brand.css` and the existing homepage primitives: warm paper (#faf7f0), ink (#48423a), brass (#8f6a3f), with their existing dark-theme equivalents. Assistant is the body face; Frank Ruhl Libre is the display face. Body is 17px, section headings 32–48px, hero 40–72px. Spacing follows 8px steps, with 24px page gutters and a 1060px content width.

Keep the real Jewish artwork (`docs/images/hero.jpg`) visible beside the hero copy. Use the existing Admin and Store screenshots as product evidence, labelled as sample screens. Keep buttons, groups and screenshot frames consistent with the existing component system. One filled exploration action and one outlined testing action lead the page.

## Content and interaction

Order: family promise and early-stage notice; everyday benefits; parent app; family group examples; store and search; professional direction; testing invitation and status. Preserve the keyboard-operable example chooser, explicitly described as illustrative family-defined groups. No invented endorsements, compatibility claims, numbers, or release dates.

The homepage remains MkDocs' `home.html`; other documentation retains its layout. All local links use the theme URL filter. Content is readable without JavaScript, reveal motion respects reduced motion, focus stays visible, and the layout stacks at phone widths. Avoid decorative perpetual animation.

## Experimental feature handoff

`docs/homepage-readiness.md` records features to reconsider before launch, their evidence requirements and proposed homepage placement. Experimental AI filtering is excluded from supported-feature copy until live compatibility is established.
