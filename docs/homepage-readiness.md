# Homepage launch checklist

Maintainer notes for revisiting marketing claims before a release. The homepage leads with a better computer for frum families. Version 1 focuses on families; professional daily use and further quality-of-life improvements are longer-term goals.

## Experimental features to revisit

- [ ] **AI response filtering (ChatGPT and Claude).** Experimental work exists, but browser and desktop compatibility is not certified. Before adding it to the homepage, verify authenticated live clients, streaming, conversation history, generated images, reconnects, citations and tool/artifact behavior. Review the experimental implementation's documented exclusions. Describe only the verified scope; do not imply universal AI compatibility or complete generated-content coverage. Candidate placement: the filtering section, with a link to the feature's documentation once merged.
- [ ] **Professional workflows.** Gather end-to-end reports from writers, teachers, graphic artists and developers. Name tested software, versions, peripherals and limitations before advertising support for a profession's workflow. Candidate placement: “For what you're making next.”

## Before changing the early-stage notice

- [ ] Confirm reliability on real machines, setup, updates, rollback and filter enforcement. Replace pre-alpha wording only when release readiness has actually changed.
- [ ] Publish a versioned x86_64 installer ISO with verification instructions, confirm its size and required USB capacity, and rehearse the [USB guide](install.md) from Windows and macOS through installation on a spare PC. Remove the availability notice only when an installer can actually be downloaded.
- [ ] Measure full-system RAM, disk use, update headroom and picture-checking performance on minimum and recommended hardware before certifying the provisional requirements in the USB guide.
- [ ] Refresh screenshots from the release being promoted. Label sample accounts and illustrative groups accurately.
- [ ] Recheck supported apps, store rules, local-processing/privacy claims and update behavior against the released implementation. Parents apply OS updates; do not promise automatic installation.
- [ ] Review new quality-of-life features for the everyday-family section. Keep future plans distinct from features available in the release.

## Sources for the current page

[Overview](overview.md), [supported tools and limits](supported.md), [time limits](time-limits.md), [content filtering](content-filtering.md), [deployment](deployment.md), and [contributing](contributing.md). The unmerged AI work is intentionally a reminder here rather than a supported-feature claim on the homepage.
