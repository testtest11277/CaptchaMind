# Experimental Subjects

This directory contains the Android application subjects used in the CAPTCHA solving experiments.

In this study, a subject refers to an Android application and its tested CAPTCHA scenario, rather than a human participant. Each subject was evaluated in 20 trials.

## File

- `subjects.csv`: application-level subject information and solving results.

## Column description

| Column | Description |
| --- | --- |
| `subject_id` | Subject identifier used in the experiment |
| `captcha_type` | CAPTCHA category |
| `app_name` | Android application name |
| `app_version` | Application version used in the experiment |
| `reported_size` | Application size as reported in the experiment table |

The size strings and challenge descriptions are preserved from the experimental table. The size unit should be defined explicitly in the paper or artifact metadata if the original measurement convention is available.

No human participant information is included in this directory.

