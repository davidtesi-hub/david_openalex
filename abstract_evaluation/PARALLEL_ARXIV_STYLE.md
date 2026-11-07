# Evaluación paralela con el funcionamiento de arXiv

Los workflows `openalex-abstract-importance-parallel-qwen3_8b.yml` y
`openalex-abstract-importance-parallel-openscholar_8b.yml` son independientes
de los evaluadores anteriores. Se ejecutan manualmente o con
`repository_dispatch`; no tienen cron mientras se comparan los resultados.

Leen `papers.abstract` de `library/<revista>.sqlite`, solo para artículos
con abstract y no retractados. Ese campo es el texto que el collector
reconstruye desde `abstract_inverted_index` de OpenAlex. Seleccionan por
`publication_date`, rotan revistas por día y aceptan prioridades separadas
en `priority_arxiv_style_mod0.json` y `priority_arxiv_style_mod1.json`.

El prompt se copia literalmente del evaluador v3 de arXiv. Qwen recibe
además `/no_think`. El normalizador y la puntuación usan el mismo código
que arXiv: una respuesta de 13 líneas, preguntas Q1–Q4 con pesos
20/40/20/20, nota entera en centésimas de 0 a 1000 o NULL si aparece U.
Cada modelo escribe una SQLite distinta con las columnas `id` (ID corto
de OpenAlex), `modelo`, `version_rubrica`, `puntuacion`, `T` y `Q1` a `J4`.
Una respuesta inválida no crea fila y queda pendiente para otra ejecución.

Los workflows publican sus SQLite con `push` y no crean artefactos.
La ejecución manual permite limitar el número de artículos con `max_papers`.
