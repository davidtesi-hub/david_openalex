# david_openalex

Colector incremental de metadatos OpenAlex para revistas seleccionadas.

La primera revista es **Astronomy & Astrophysics**, source `S205231332`, ISSN
0004-6361 / 1432-0746. Solo se incluyen works `type:article` cuya fuente primaria
es la revista. Se consulta el corpus core por defecto. No se descargan PDFs.

## Ejecutar

Python 3.12 y biblioteca estándar, sin dependencias externas. Configurar
`OPENALEX_API_KEY` en el entorno (localmente) o como secret del repo en Actions.
La clave nunca debe guardarse en Git. El PAT de GitHub es una credencial distinta.

```sh
python scripts/collect_openalex_journal.py --journal aanda --max-intervals 2 --max-requests 100
```

Prueba real acotada a una fecha de publicación:

```sh
python scripts/collect_openalex_journal.py --journal aanda --from-date 2026-10-01 --to-date 2026-10-01 --max-intervals 1 --max-requests 5
```

En GitHub Actions, lanzar manualmente **Astronomy & Astrophysics collector**.
Cada revista tiene cron activo cada cuatro horas, escalonado diez minutos en UTC. El límite interno es 25 minutos y el job tiene
30 minutos. Los intentos HTTP, descubrimiento y reintentos cuentan para el máximo.

## Datos y cobertura

`library/aanda.sqlite` guarda papers (campos aprobados y columnas consultables),
cobertura diaria, ejecuciones, estado y registro de llamadas sin secretos.
`library/aanda.coverage.json` es un manifiesto generado desde esa base.

Se consultan intervalos de hasta **120 dias de publicacion**, hasta ayer UTC,
con cursor y hasta 100 works por pagina. Una solicitud simultanea y un segundo
minimo entre llamadas. `--max-intervals` limita bloques, no peticiones.
Por defecto se intentan dos bloques: el reciente para refresco y otro historico.
`--interval-days` permite reducir el bloque (120 por defecto).

Solo se marca un intervalo completo al agotar sus paginas y comprobar IDs unicos
y conteos. Cada articulo conserva su fecha individual. Una interrupcion conserva
las paginas; el intervalo se reinicia con UPSERT en la siguiente ejecucion.

El esquema 2 conserva day_coverage como historial diario y agrega interval_coverage.
El manifiesto contiene legacy_days e intervals. Los huecos historicos se agrupan
en periodos contiguos de hasta 120 dias; luego se reconcilian intervalos antiguos.
Las altas tardias se detectan al volver a consultar sus fechas. El limite historico
guardado en collector_state debe revisarse si se incorporan obras anteriores.
No se usan filtros premium de actualizacion.

La seleccion persistida esta definida en `approved_fields.json`. `raw_json` contiene
solo los campos aprobados, proyectados y normalizados; ya no es el JSON original
integro de OpenAlex. Los Work/Source/Author IDs pierden el prefijo OpenAlex y los
DOI pierden https://doi.org/. Las URLs de acceso se mantienen completas. Los temas
conservan solo nombres; los autores conservan author.id. Los campos ausentes se
omiten y los valores nulos se mantienen.

La migracion automatica de esquema 3 adapta las filas antiguas en una transaccion,
sin peticiones a OpenAlex. Conserva first_seen_utc y last_seen_utc como metadatos
operativos del colector. La cobertura y el registro de peticiones no se proyectan.
Las respuestas de lista pueden limitar a 100 las autorias y ubicaciones mostradas;
no se solicita ampliacion individual automatica. No se borran articulos por su
ausencia posterior; los cambios de fuente/tipo requieren reconciliacion especifica.

## Límites y errores

429 detiene conservadoramente el run, tanto si es cuota como si es ritmo.
401/403 son errores de autenticación/autorización. Errores de red/5xx se reintentan
con backoff y Retry-After, hasta tres reintentos, sujetos al máximo de intentos.
Un límite local deja el run `stopped`; un error de datos o servicio deja `error`.
El coste reportado se registra cuando está disponible. **Usar la API consume
presupuesto**; contar peticiones no garantiza un coste monetario máximo y la
cuota puede compartirse con otros clientes de la misma cuenta. No se compran
recargas ni se modifican planes.

Actions guarda un artefacto y publica SQLite/cobertura después de cerrar las
conexiones. Si detecta que los datos A&A cambiaron en remoto, conserva el artefacto
y falla la publicación en lugar de sobrescribirlos. Una terminación forzada del
runner puede perder cambios aún no guardados/publicados. Los cron pendientes no
son una cola fiable; la base de cobertura permite recuperar huecos.

## Comprobaciones locales sin consumo de API

```sh
python -m unittest discover -s tests -v
```

## Referencias

- [Autenticación y límites](https://help.openalex.org/api/authentication/)
- [Paginación](https://help.openalex.org/api/paging/)
- [Filtros](https://help.openalex.org/api/filtering/)
- [Precios](https://help.openalex.org/access/pricing/)
- [Campos Work](https://help.openalex.org/data/works/attributes/)

Arquitectura inspirada en `oliverscott-hub/oliver_arxiv`: cobertura diaria,
SQLite por unidad de recolección y workflow manual por revista.

## Revistas y horarios UTC

El orden del catalogo sigue la seleccion del usuario. El cron es ciclico: por ejemplo,
una fase de 04:00 incluye tambien 00:00. GitHub puede retrasar u omitir ejecuciones
programadas; la cobertura persistente recupera los huecos. Todos los colectores
comparten el grupo openalex-collectors y los limites conservadores de peticiones.
Cada revista conserva su SQLite y manifiesto propios, creados en su primera captura.

| Orden | Codigo | Revista | Cron UTC |
|---:|---|---|---|
| 1 | apj | The Astrophysical Journal | `0 3,7,11,15,19,23 * * *` |
| 2 | apjl | The Astrophysical Journal Letters | `10 3,7,11,15,19,23 * * *` |
| 3 | aj | The Astronomical Journal | `20 3,7,11,15,19,23 * * *` |
| 4 | pasp | Publications of the Astronomical Society of the Pacific | `30 3,7,11,15,19,23 * * *` |
| 5 | aanda | Astronomy & Astrophysics | `40 3,7,11,15,19,23 * * *` |
| 6 | mnras | Monthly Notices of the Royal Astronomical Society | `50 3,7,11,15,19,23 * * *` |
| 7 | natas | Nature Astronomy | `0 0,4,8,12,16,20 * * *` |
| 8 | jhep | Journal of High Energy Physics | `10 0,4,8,12,16,20 * * *` |
| 9 | epjc | European Physical Journal C | `20 0,4,8,12,16,20 * * *` |
| 10 | cmpa | Communications in Mathematical Physics | `30 0,4,8,12,16,20 * * *` |
| 11 | lmpa | Letters in Mathematical Physics | `40 0,4,8,12,16,20 * * *` |
| 12 | phrvd | Physical Review D | `50 0,4,8,12,16,20 * * *` |
| 13 | phrvl | Physical Review Letters | `0 1,5,9,13,17,21 * * *` |
| 14 | phlb | Physics Letters B | `10 1,5,9,13,17,21 * * *` |
| 15 | nuphb | Nuclear Physics B | `20 1,5,9,13,17,21 * * *` |
| 16 | cqgra | Classical and Quantum Gravity | `30 1,5,9,13,17,21 * * *` |
| 17 | jcap | Journal of Cosmology and Astroparticle Physics | `40 1,5,9,13,17,21 * * *` |
| 18 | jmp | Journal of Mathematical Physics | `50 1,5,9,13,17,21 * * *` |
| 19 | sci | SciPost Physics | `0 2,6,10,14,18,22 * * *` |
| 20 | mplb | Modern Physics Letters B | `10 2,6,10,14,18,22 * * *` |
