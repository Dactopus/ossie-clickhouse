-- Expected answers for cases-two-facts.json, written by hand without the
-- planner. C*: questions the executor answers. B*: questions it refuses with
-- UNSUPPORTED_QUERY that #246 answers by aggregating each fact on its own and
-- joining the results on the dimensions (step A.7, section 6.6).

-- C1 revenue by shipping country, having revenue > 100
SELECT 'C1', shipping_country, sum(total) AS revenue FROM ossie_profile.orders
GROUP BY shipping_country HAVING revenue > 100 ORDER BY revenue DESC;
-- C2 items sold by channel, having items_sold >= 3
SELECT 'C2', o.channel, sum(l.quantity) AS items_sold FROM ossie_profile.order_lines AS l
LEFT JOIN ossie_profile.orders AS o ON l.order_id = o.order_id
GROUP BY o.channel HAVING items_sold >= 3 ORDER BY items_sold DESC SETTINGS join_use_nulls = 1;
-- C5 revenue by channel, having channel <> 'pos' and revenue > 0
SELECT 'C5', channel, sum(total) AS revenue FROM ossie_profile.orders
GROUP BY channel HAVING channel <> 'pos' AND revenue > 0 ORDER BY revenue DESC;
-- C7 revenue by channel, having orders > 2
SELECT 'C7', channel, sum(total) AS revenue FROM ossie_profile.orders
GROUP BY channel HAVING count(order_id) > 2 ORDER BY revenue DESC;
-- B1 items sold and refunds by channel (B2 adds revenue, read at the order grain)
SELECT 'B1/B2', c.channel, r.revenue, li.items_sold, rf.refunds_total
FROM (SELECT DISTINCT channel FROM ossie_profile.orders) AS c
LEFT JOIN (SELECT channel, sum(total) AS revenue FROM ossie_profile.orders GROUP BY channel) AS r
    ON r.channel = c.channel
LEFT JOIN (SELECT o.channel, sum(l.quantity) AS items_sold FROM ossie_profile.order_lines AS l
           LEFT JOIN ossie_profile.orders AS o ON l.order_id = o.order_id GROUP BY o.channel) AS li
    ON li.channel = c.channel
LEFT JOIN (SELECT o.channel, sum(f.refunded_amount) AS refunds_total FROM ossie_profile.refunds AS f
           LEFT JOIN ossie_profile.orders AS o ON f.order_id = o.order_id GROUP BY o.channel) AS rf
    ON rf.channel = c.channel
ORDER BY c.channel SETTINGS join_use_nulls = 1;
-- B3 web revenue and page views for the top 5 sources by web revenue
SELECT 'B3', s.source, p.web_revenue, s.page_views
FROM (SELECT source, sum(page_views) AS page_views FROM dactopus.sessions GROUP BY source) AS s
LEFT JOIN (SELECT s.source, sum(p.revenue) AS web_revenue FROM dactopus.purchases AS p
           LEFT JOIN dactopus.sessions AS s ON p.session_key = s.session_key GROUP BY s.source) AS p
    ON p.source = s.source
ORDER BY p.web_revenue DESC NULLS LAST LIMIT 5 SETTINGS join_use_nulls = 1;
-- B4 items sold and refunds in total
SELECT 'B4', (SELECT sum(quantity) FROM ossie_profile.order_lines),
       (SELECT sum(refunded_amount) FROM ossie_profile.refunds);
