export const WELLBEING_TEST_KINDS = [
  'long_work',
  'commute_safety',
  'overtime',
  'frantic_overtime',
  'warm_encouragement',
] as const;

export type WellbeingTestKind = typeof WELLBEING_TEST_KINDS[number];
