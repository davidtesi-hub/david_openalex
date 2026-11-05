# Rúbrica v1 para abstracts de OpenAlex

Cada Work ID con abstract de los 20 colectores de revistas recibe tres evaluaciones independientes por modelo: A (siete criterios y explicación global), B (los mismos criterios y evidencias por criterio) y C (nota directa de referencia). Los modelos son Qwen3-8B y OpenScholar-8B. No se consulta la API OpenAlex para evaluar.

Las bases `rubric_qwen3_8b.sqlite` y `rubric_openscholar_8b.sqlite` son nuevas. Las bases antiguas `abstract_importance_mod0.sqlite` y `abstract_importance_mod1.sqlite` se conservan. `evaluations` contiene primero `work_id`, `modelo`, `version_rubrica`, `nota_milesimas`; 7167 significa 7.167/10. Cada intento se añade como fila, con `variante`, versión y pesos, SHA-256 del abstract, I1–I7, evidencias, metadatos descriptivos, JSON original, error de validación, UTC, latencia y tokens. La nota queda nula si la respuesta no valida. En C no hay dimensiones.

La nota A/B se calcula por código con pesos 15,15,25,10,15,10,10 y categorías 0–3. Las opciones indeterminadas quedan explícitas, junto con cobertura y cota mínima/máxima. Cambiar pesos requiere nueva versión de pesos; cambiar definiciones, nueva versión de rúbrica.

Se seleccionan artículos `type=article` con abstract, no retractados, y se deduplican por Work ID. Se priorizan los Work IDs de `priority_mod0.json` o `priority_mod1.json`; luego se recorren fechas de publicación recientes alternando revistas. Los workflows corren a las 02:00, 10:00 y 18:00 UTC, manualmente y por `repository_dispatch`. Cada modelo usa su propia base y grupo de concurrencia.

```sql
SELECT work_id, modelo, version_rubrica,
       printf('%.3f',nota_milesimas/1000.0) AS nota,
       variante,I1,I2,I3,I4,I5,I6,I7
FROM evaluations WHERE validation_error IS NULL;
```
