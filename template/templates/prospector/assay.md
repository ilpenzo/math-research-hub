# Prospector assay: try to kill these cards

Other explorers proposed the claim cards below. Your job is to find, quickly,
the decisive reason each one fails, or to report that it survives your best
attempt. Be adversarial but fair.

For each card check, in order of cost:

1. Is it circular, a restatement of the target, or an assumption equivalent
   to the gap?
2. Does it contradict a recorded obstruction, correction or control listed in
   the frontier? Check the obstruction's exact hypotheses before using it.
3. Is it already a recorded result (then it is not progress)?
4. Does its weakest step fail on an explicit example satisfying all of the
   card's hypotheses? {{tool_rule}}

A control outside the card's hypotheses is a diagnostic, not a kill. A finite
search that finds nothing does not prove the card. If a card fails but a
modified statement survives, put the modification in `salvage`; if it is
worth pursuing, also return it as a new card.

Return exactly one verdict per card, using the card IDs shown: `killed`,
`survives` or `unclear`. Do not run Git, edit files outside your working
directory or publish anything.

{{frontier}}

## Cards to assay

{{cards}}

{{context}}

{{output_contract}}
