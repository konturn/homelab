<?php
/**
 * Plugin Name: MathJax for math posts
 * Description: Loads MathJax on singular posts that contain TeX ($...$, \[...\]). Client-side only.
 */
add_action('wp_enqueue_scripts', function () {
    if (!is_singular()) return;
    $c = get_post_field('post_content', get_queried_object_id());
    if (strpos($c, '$') === false && strpos($c, '\\[') === false && strpos($c, '\\(') === false) return;
    wp_add_inline_script('mathjax', 'window.MathJax={tex:{inlineMath:[["$","$"],["\\\\(","\\\\)"]],displayMath:[["$$","$$"],["\\\\[","\\\\]"]],processEscapes:true}};', 'before');
    wp_enqueue_script('mathjax', 'https://cdn.jsdelivr.net/npm/mathjax@3.2.2/es5/tex-chtml.js', [], null, ['strategy' => 'defer']);
});
