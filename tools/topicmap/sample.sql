copy (
  select id, coalesce(title,'') title, vertical, coalesce(array_to_string(tags[1:3], '|'),'') tags,
         coalesce(personal_score,0) score, coalesce(url,'') url, coalesce(feed_name,'') feed,
         to_char(coalesce(published_at, fetched_at), 'YYYY-MM-DD') as dt
  from news_items
  where processed_at is not null and skip_reason is null and vertical in ('business','it','medical')
    and embedding_version is not null
    and coalesce(published_at, fetched_at) > now() - interval '90 days'
  order by random() limit 20000
) to stdout with csv header
