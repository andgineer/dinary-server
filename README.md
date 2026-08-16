# Repository Coverage

[Full report](https://htmlpreview.github.io/?https://github.com/andgineer/dinary/blob/python-coverage-comment-action-data/htmlcov/index.html)

| Name                                                           |    Stmts |     Miss |   Cover |   Missing |
|--------------------------------------------------------------- | -------: | -------: | ------: | --------: |
| src/dinary/\_\_about\_\_.py                                    |        1 |        0 |    100% |           |
| src/dinary/adapters/rates/helpers.py                           |       27 |        0 |    100% |           |
| src/dinary/adapters/rates/nbp.py                               |       37 |        0 |    100% |           |
| src/dinary/adapters/rates/nbs.py                               |       47 |        0 |    100% |           |
| src/dinary/adapters/rates/service.py                           |       43 |        1 |     98% |        32 |
| src/dinary/adapters/receipts/dispatch.py                       |       22 |        0 |    100% |           |
| src/dinary/adapters/receipts/montenegrin.py                    |       90 |        9 |     90% |84-85, 87, 94, 102, 117, 178-179, 193 |
| src/dinary/adapters/receipts/serbian.py                        |      116 |       18 |     84% |76, 82, 91-97, 111, 118, 152-153, 157-158, 161, 186-188, 215-221 |
| src/dinary/adapters/receipts/types.py                          |       14 |        0 |    100% |           |
| src/dinary/adapters/sheets\_client.py                          |       44 |       28 |     36% |26-35, 39, 51-60, 69-81 |
| src/dinary/api/analytics.py                                    |       52 |        2 |     96% |    33, 36 |
| src/dinary/api/catalog.py                                      |       81 |        9 |     89% |68-70, 105-113, 158-169 |
| src/dinary/api/category\_templates.py                          |       33 |        0 |    100% |           |
| src/dinary/api/controllers/catalog.py                          |      136 |        0 |    100% |           |
| src/dinary/api/controllers/catalog\_writer.py                  |       33 |        1 |     97% |        17 |
| src/dinary/api/controllers/catalog\_writer\_errors.py          |       31 |        2 |     94% |     55-56 |
| src/dinary/api/controllers/catalog\_writer\_events.py          |      187 |       33 |     82% |35, 184-186, 201, 211-216, 243, 245, 247, 249, 259, 275, 311-313, 332, 339, 349-351, 378, 382-384, 391-393, 410, 423-425 |
| src/dinary/api/controllers/catalog\_writer\_groups.py          |       76 |       31 |     59% |25-29, 90-92, 105-145, 154, 171 |
| src/dinary/api/controllers/category\_templates.py              |       80 |        1 |     99% |       118 |
| src/dinary/api/controllers/correction\_ratings.py              |       35 |        2 |     94% |     41-42 |
| src/dinary/api/controllers/expense\_corrections.py             |       76 |        1 |     99% |        98 |
| src/dinary/api/controllers/expenses.py                         |      176 |       17 |     90% |226-227, 263-273, 322, 400-402, 415-417 |
| src/dinary/api/controllers/income.py                           |       57 |        2 |     96% |   134-135 |
| src/dinary/api/controllers/llm.py                              |       44 |        3 |     93% | 25, 88-89 |
| src/dinary/api/controllers/receipt\_queue.py                   |       68 |        3 |     96% |99, 122, 197 |
| src/dinary/api/controllers/rules.py                            |       58 |        2 |     97% |     42-43 |
| src/dinary/api/currencies.py                                   |       33 |        0 |    100% |           |
| src/dinary/api/expense\_corrections.py                         |       13 |        0 |    100% |           |
| src/dinary/api/expenses.py                                     |       28 |        0 |    100% |           |
| src/dinary/api/http\_errors.py                                 |        9 |        0 |    100% |           |
| src/dinary/api/income.py                                       |       26 |        2 |     92% |    40, 52 |
| src/dinary/api/llm.py                                          |       20 |        0 |    100% |           |
| src/dinary/api/receipts.py                                     |       58 |        5 |     91% |   108-113 |
| src/dinary/api/rules.py                                        |       23 |        0 |    100% |           |
| src/dinary/background/classification/item\_normalizer.py       |       13 |        0 |    100% |           |
| src/dinary/background/classification/persist.py                |       83 |        1 |     99% |       155 |
| src/dinary/background/classification/receipt\_classifier.py    |       58 |        0 |    100% |           |
| src/dinary/background/classification/store\_resolver.py        |       31 |        1 |     97% |        69 |
| src/dinary/background/classification/task.py                   |      267 |       21 |     92% |80, 133-134, 140-147, 215-229, 332, 381, 496-497, 542 |
| src/dinary/background/rate\_prefetch/task.py                   |       51 |        2 |     96% |    59, 87 |
| src/dinary/background/sheet\_logging/income\_sheet\_logging.py |      177 |       44 |     75% |59-64, 90-91, 98-99, 107-109, 120, 153, 166-169, 177-180, 205-206, 218, 235, 241-243, 271-278, 290, 294, 318-326 |
| src/dinary/background/sheet\_logging/logging\_jobs.py          |       63 |        9 |     86% |78-79, 95-101 |
| src/dinary/background/sheet\_logging/sheet\_logging.py         |      258 |       37 |     86% |94-98, 118-119, 127, 129, 151, 229-230, 240, 245-247, 290-292, 315-316, 320-330, 439-440, 451, 455, 487-496, 517-518 |
| src/dinary/background/sheet\_logging/sheets\_write.py          |       65 |       35 |     46% |106-111, 116-119, 123-128, 143-177 |
| src/dinary/background/sheet\_logging/task.py                   |       59 |        8 |     86% | 29-42, 79 |
| src/dinary/category\_templates/loader.py                       |       65 |        0 |    100% |           |
| src/dinary/config.py                                           |       69 |        6 |     91% |30, 40, 79, 91-93 |
| src/dinary/db/catalog.py                                       |      119 |        1 |     99% |        86 |
| src/dinary/db/category\_apply.py                               |       40 |        0 |    100% |           |
| src/dinary/db/category\_seed.py                                |       59 |        6 |     90% |141-148, 153 |
| src/dinary/db/classification\_rules.py                         |       38 |        2 |     95% |     67-68 |
| src/dinary/db/currencies.py                                    |       34 |        5 |     85% |15-16, 53-58 |
| src/dinary/db/db\_migrations.py                                |       56 |        2 |     96% |    53, 56 |
| src/dinary/db/expenses.py                                      |      130 |       12 |     91% |126, 168, 201-208, 227-233, 316, 420, 462 |
| src/dinary/db/income.py                                        |       55 |        5 |     91% |103, 117-124 |
| src/dinary/db/receipts.py                                      |       92 |        3 |     97% |   119-121 |
| src/dinary/db/sql\_loader.py                                   |       31 |        0 |    100% |           |
| src/dinary/db/storage.py                                       |      129 |        7 |     95% |63, 234-237, 245-251 |
| src/dinary/main.py                                             |      101 |        8 |     92% |44-46, 139, 145, 151, 161, 171 |
| src/dinary/sheets/sheet\_mapping.py                            |      224 |       47 |     79% |224, 289-291, 308-309, 316-322, 369, 372-377, 391-392, 402-420, 434-462, 478, 489-494, 499-500 |
| src/dinary/sheets/sheets.py                                    |      104 |        6 |     94% |51-52, 71, 100, 118, 201 |
| src/dinary\_analytics/ai\_service.py                           |       77 |       10 |     87% |53, 59, 132-138, 142 |
| src/dinary\_analytics/backup.py                                |       66 |       21 |     68% |18-19, 36-37, 54-55, 69-87, 91 |
| src/dinary\_analytics/charts.py                                |       57 |        0 |    100% |           |
| src/dinary\_analytics/connection.py                            |       11 |        0 |    100% |           |
| src/dinary\_analytics/llm.py                                   |       61 |        4 |     93% |66, 69, 129-130 |
| src/dinary\_analytics/notebooks/dashboard.py                   |      425 |      166 |     61% |32, 76, 85, 98, 115, 131-134, 148-190, 212-214, 216, 220-221, 270-340, 352, 443, 455-525, 538-549, 554-584, 589-619, 631-644, 649-651, 683-691, 702, 707-711, 716-720, 746, 762, 895, 911-913, 980-1016, 1021-1044, 1065-1068, 1072-1076, 1080-1084, 1088-1090, 1104-1105, 1110, 1149 |
| src/dinary\_analytics/paths.py                                 |       17 |        0 |    100% |           |
| src/dinary\_analytics/refresh.py                               |      100 |        2 |     98% |   50, 157 |
| src/dinary\_analytics/settings.py                              |       49 |        1 |     98% |        56 |
| src/dinary\_analytics/views.py                                 |       23 |        1 |     96% |        62 |
| **TOTAL**                                                      | **5231** |  **645** | **88%** |           |


## Setup coverage badge

Below are examples of the badges you can use in your main branch `README` file.

### Direct image

[![Coverage badge](https://raw.githubusercontent.com/andgineer/dinary/python-coverage-comment-action-data/badge.svg)](https://htmlpreview.github.io/?https://github.com/andgineer/dinary/blob/python-coverage-comment-action-data/htmlcov/index.html)

This is the one to use if your repository is private or if you don't want to customize anything.

### [Shields.io](https://shields.io) Json Endpoint

[![Coverage badge](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/andgineer/dinary/python-coverage-comment-action-data/endpoint.json)](https://htmlpreview.github.io/?https://github.com/andgineer/dinary/blob/python-coverage-comment-action-data/htmlcov/index.html)

Using this one will allow you to [customize](https://shields.io/endpoint) the look of your badge.
It won't work with private repositories. It won't be refreshed more than once per five minutes.

### [Shields.io](https://shields.io) Dynamic Badge

[![Coverage badge](https://img.shields.io/badge/dynamic/json?color=brightgreen&label=coverage&query=%24.message&url=https%3A%2F%2Fraw.githubusercontent.com%2Fandgineer%2Fdinary%2Fpython-coverage-comment-action-data%2Fendpoint.json)](https://htmlpreview.github.io/?https://github.com/andgineer/dinary/blob/python-coverage-comment-action-data/htmlcov/index.html)

This one will always be the same color. It won't work for private repos. I'm not even sure why we included it.

## What is that?

This branch is part of the
[python-coverage-comment-action](https://github.com/marketplace/actions/python-coverage-comment)
GitHub Action. All the files in this branch are automatically generated and may be
overwritten at any moment.