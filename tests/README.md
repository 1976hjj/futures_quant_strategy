# Test layout

- `unit/`: pure calculations and schema behavior;
- `integration/`: boundaries between data, factor, evaluator and execution;
- `golden/`: independently specified small datasets and expected results;
- `audit/`: adversarial tests for leakage, PIT, Universe, execution and Holdout;
- `crosscheck/`: numeric comparisons with independent external implementations.

Golden expectations must not be generated solely by the implementation under test. Each golden case must state its independent derivation.

