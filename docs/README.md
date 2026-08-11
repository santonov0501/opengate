# OpenGate Documentation

Документация проекта OpenGate, построенная на Docusaurus.

## Локальная разработка

```bash
cd docs
npm install
npm run start
```

Документация будет доступна на `http://localhost:3000`.

## Сборка

```bash
cd docs
npm run build
```

Готовые файлы будут в папке `docs/build`.

## Деплой

Документация автоматически деплоится на GitHub Pages при пуше в ветку `main`.

## Структура

```
docs/
├── docs/                    # Markdown файлы документации
│   ├── intro.md            # Введение
│   ├── architecture.md     # Архитектура
│   └── api.md              # API документация
├── src/                     # Статические ресурсы
│   └── css/
│       └── custom.css      # Кастомные стили
├── docusaurus.config.js     # Конфигурация Docusaurus
├── sidebars.js              # Настройка сайдбара
├── package.json             # Зависимости
└── babel.config.js          # Babel конфигурация