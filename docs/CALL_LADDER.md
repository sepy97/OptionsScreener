# The call ladder

Covered calls on shares that came from an assigned put, stepped up a fixed set of strikes (the
**rungs**) until the shares can leave at what they cost. Worked out on TER: 100 shares assigned at
$390 with TER near $270, a $340 call sold for about $6, and rungs at $340, $355, $370 and $390. The
Call ladder tab prices a plan like that against the chain; `core/ladder.py` holds the rules.

## Why fixed rungs

A strike picked by delta at roll time can't be ordered in advance. A fixed next rung can, so each
call carries two resting orders at the broker and needs a person only when they can't act.

The first rung is the worst exit: a jump past it strands the call there. Starting the rungs at a
floor (the $340 the TER plan would accept being called at) rather than at the share price is what
keeps a jump from locking in a large loss. Within each rung, the delta rises on its own as the
price approaches it: about 0.19 for the $340 call with TER at $300, 0.44 at $330.

## The two orders on each call

Linked so that one cancels the other: both buy back the same call, and two fills would leave a
long call.

- **Take profit**: buy to close at 25% of what the call was sold for, good till cancelled.
- **Roll up a rung**: a conditional order that waits for the share price to reach 97% of the
  strike, then buys the call back and sells the next rung four to six weeks after its expiry
  (the shortest that fits), for a net credit, good till cancelled.

The roll's **limit** is 85% of the lowest credit the model gives at the trigger price over the
call's remaining life, with volatility 10 points lower than now, since a rally usually lowers it.
It is never below the minimum roll credit. Near the strike the credit grows as expiry nears; far
above it the credit falls to nothing, so past the **gap price** (where, with 10 days left, the
credit drops under the limit) the order is sent but can't fill.

## Reports

No option is held over an earnings report. The last listed expiry before a report is a wall: a
roll must land before the report, so the call that expires at the wall can't roll, and only its
take-profit rests. After the report the ladder starts again. That leaves room for about two rolls
between reports: the ladder climbs at most two rungs a quarter.

## Minimums

As shares of the price, so that they hold for a $30 stock as for a $300 one (the TER plan's $3.00
and $0.50 at $300 and $340):

- a new call has to pay 1% of the share price;
- a roll has to pay 0.15% of the strike, and never a debit.

## What the page can't see

The model prices are Black–Scholes at one volatility (the open call's), at mid prices, without
dividends. A resting order fills at the bid and ask, which is lower. The page reads what the
person types: after a fill, the plan's numbers (the new call, what it sold for, the premiums) are
updated by hand. Gaps past the roll window, early assignment, the report and fills are what need a
person.
