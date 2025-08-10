---
layout: post
title: Cluster-Based Permutation Test
date: 2025-08-09 12:00:00
description: Understand a statistical test
tags: statistics math research
categories: exploration
related_posts: false
translation_id: permutation_test
lang: en
---

> Imagine you're a researcher who has just collected some amazing data, maybe EEG signals over 1000 time points or fMRI data across thousands of brain voxels. You want to see where and when your experimental condition differs from a control condition.\
The immediate temptation is to run a t-test at every single time point or voxel. This leads directly to the **multiple comparisons** problem. Introduce this problem with a simple analogy.
>* **The Jelly Bean Test**: If you test one jelly bean for being poisonous, a p-value of 0.05 is fine. But if you test 1000 jelly beans, you're almost guaranteed to find a "significant" one just by random chance, even if none are actually poisonous. This is what happens when you test thousands of data points.

Conclude the intro by positioning permutation test, is a non parametric method to deal with Multiple Comparison Problem(MCP), and is pretty common use in M/EEG data(multiple channel, multiple time and multiple frequency), because it will easily cause **type I error** which is wrongly reject null hypothesis.

---

## How It Works: A Step-by-Step Breakdown
We have a dataset from **3 people**, with recordings taken before (pre) and after (post) a meditation session. For each person, we measured the power spectrum across **5 electrode channels** and **10 frequency bins**.

This gives us a 5x10 grid of data for each person. When we calculate the pre-post difference, we have 50 statistical tests we could run (5 channels * 10 frequencies). If we run 50 separate t-tests, we run into the **multiple comparisons problem**. We are likely to find "significant" results just by random chance.

This is where the cluster-based permutation test comes in. It's designed to solve this exact problem by looking for patterns—or clusters—in the data, rather than individual points.



**Step 1: Calculate Raw Statistics** 

First, you do exactly what you initially thought of: perform a separate statistical test (like a t-test) for every single data point (e.g., each time point and sensor). This gives you a map of raw t-values.

**Step 2: Apply a Significance Threshold**

Next, we need to decide which t-values are "interesting." We do this by setting a cluster-entry threshold. This is usually an uncorrected p-value, like p<0.05. Any t-value that is more extreme than this threshold is considered a "candidate" to be part of a cluster.

All the points that don't pass this threshold are discarded. This helps us focus only on the points showing a relatively strong effect.

**Step 3: Find Clusters of Adjacent Points**

Now, we look at our thresholded map and group together any candidate points that are adjacent to each other. Adjacency can be defined in different ways, but for our 2D grid, we can say points are adjacent if they touch at the sides (not just diagonally).\
This process results in one or more "clusters"—islands of significant effects.\
In our example, we can see two distinct clusters emerge: a larger, positive cluster (in red) and a smaller, negative one (in blue). These are circled to make them clear.

<div class="row mt-3">
  <div class="col-sm-12 mt-3 mt-md-0" style="display: flex; justify-content: center;">
    {% include figure.liquid 
        loading="eager" 
        path="assets/img/blog-permut/t-table.png" 
        class="img-fluid rounded z-depth-1" 
        zoomable=true 
        style="max-width: 300px;" 
    %}
  </div>
</div>
<div class="caption">
    Figure for step1 to step 3, with black circled the MCP matrix with p-val < 0.05
</div>

**Step 4: Calculate Cluster "Mass"**

The "size" of each cluster isn't just how many points it contains; it's about the total strength of the effect within it. We calculate this by summing up all the individual t-values of the points inside each cluster. This sum is called the cluster mass or cluster-level statistic.\ 
We then take the largest of these (in absolute value) as our single, most important number from our real data. \Here, we've labeled our one clusters with their calculated mass. The red cluster has a mass of let's say 30 (t-val). This is our *observed_max_cluster_mass*.

**Step 5: Replacement and clustering statistics**

So, is a cluster mass of 30 big? Or could we get a cluster that large just by chance?\
To find out, we create a null distribution. We pretend the null hypothesis is true (that meditation has no effect). We do this by randomly shuffling our data. For each of our 3 subjects, we randomly either keep their "pre" and "post" labels as they are, or we swap them.

With this randomly shuffled data, we repeat the entire process:

* Calculate new t-values.

* Threshold them.

* Find clusters.

* Find the maximum cluster mass on the shuffled data.

We do this over and over again—typically 1,000 or more times. This gives us 1,000+ "maximum cluster mass" values that could have been found under the null hypothesis.\
Plotting all those random maximum cluster values gives us a histogram—our null distribution. We can then see where our actual observed value falls on this distribution. Is it in the main body of the distribution, or is it an outlier?

**Step 6: Calculate the Final p-value**

The final step is wonderfully simple. We just calculate the proportion of random shuffles that resulted in a maximum cluster mass greater than or equal to our observed one.

If we ran 1,000 permutations and only 20 of them produced a max cluster mass of 30 or more, our p-value would be 20 / 1000 = 0.02.

This p-value applies to the entire cluster.
<div class="row mt-3">
  <div class="col-sm-12 mt-3 mt-md-0" style="display: flex; justify-content: center;">
    {% include figure.liquid 
        loading="eager" 
        path="assets/img/blog-permut/p-val.jpeg" 
        class="img-fluid rounded z-depth-1" 
        zoomable=true 
        style="max-width: 300px;" 
    %}
  </div>
</div>
<div class="caption">
    Figure for step1 to step 3, with black circled the MCP matrix with p-val < 0.05
</div>
This final image shows our null distribution, with the area corresponding to the p-value shaded in. This shaded area represents the "suprise factor"—how unlikely our result was under the null hypothesis.

## Conclusion: What It All Means

So, what can we conclude from our analysis?

With a final cluster-p-value of 0.02, we can confidently say that the positive cluster we observed is statistically significant. Our interpretation would be:

"We found a significant positive cluster (p=0.02) indicating that meditation led to an increase in power. This effect was most prominent in the frontal an central channels and across the 5Hz to 8Hz frequency range."

Crucially, we cannot pick a single point within that cluster (e.g., Cz, Frequency 7) and say it is significant. The test only gives us confidence in the cluster as a whole. This is the trade-off: we lose spatial/focal precision but gain a huge amount of statistical power and solve the multiple comparisons problem.