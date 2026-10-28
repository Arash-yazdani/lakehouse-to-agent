-- Hand-written demo queries against retail.monthly_sales (Census retail sales, $ millions).

-- 1. Year-over-year growth by store type for the latest month, fastest first.
WITH latest AS (SELECT max(month) AS m FROM monthly_sales)
SELECT cur.category,
       cur.sales_musd                                         AS sales_latest,
       prev.sales_musd                                        AS sales_year_ago,
       round(100.0 * (cur.sales_musd - prev.sales_musd) / prev.sales_musd, 1) AS yoy_pct
FROM monthly_sales cur
JOIN monthly_sales prev
  ON prev.series_id = cur.series_id
 AND prev.month = cur.month - INTERVAL 1 YEAR
WHERE cur.month = (SELECT m FROM latest)
ORDER BY yoy_pct DESC;

-- 2. Share of total sales by store type, trailing 12 months (window function).
SELECT category,
       sum(sales_musd)                                                AS ttm_sales,
       round(100.0 * sum(sales_musd) / sum(sum(sales_musd)) OVER (), 1) AS share_pct
FROM monthly_sales
WHERE month > (SELECT max(month) FROM monthly_sales) - INTERVAL 12 MONTH
GROUP BY category
ORDER BY ttm_sales DESC;

-- 3. Nonstore (e-commerce) vs department stores: the crossover month.
SELECT min(month) AS first_month_nonstore_exceeded_dept
FROM (SELECT month,
             max(sales_musd) FILTER (WHERE series_id = 'RSNSR') AS nonstore,
             max(sales_musd) FILTER (WHERE series_id = 'RSDBS') AS dept
      FROM monthly_sales GROUP BY month)
WHERE nonstore > dept;
