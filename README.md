# Дашборды клиентов

Каждые 15 минут GitHub Actions запускает `collect.py`: он тянет рекламу из Meta и сделки из CRM
по каждому `projects/*.json` и публикует страницу `template/index.html` на GitHub Pages
по адресу `/<slug>/`. Цифры в репозиторий не коммитятся, живут только в опубликованном сайте.

## Новый проект
1. Скопировать `projects/gl-franchise.json`, поменять `slug` (со случайным хвостом), кабинеты, фильтр кампаний, нормы.
2. Если есть amoCRM: `crm.type = "amo"`, домен, `pipeline_id`, статусы этапов, секрет с токеном (имя в `token_env`) и строка в `.github/workflows/build.yml`.
3. Закоммитить. Ссылка: `https://kostagency.github.io/dash/<slug>/`.

Секреты репозитория: `META_ACCESS_TOKEN`, `META_APP_SECRET`, `AMO_TOKEN_*`.
