from django.db import models


class DataSource(models.Model):
    name = models.CharField(max_length=200)
    source_file_name = models.CharField(max_length=255)
    source_url = models.URLField(max_length=1000, blank=True, default='')
    description = models.TextField(blank=True, default='')
    imported_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class CrimeImportRun(models.Model):
    status = models.CharField(max_length=50, default='pending')
    notes = models.TextField(blank=True, default='')
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    suburb_count = models.IntegerField(default=0)
    precinct_count = models.IntegerField(default=0)
    crime_metric_count = models.IntegerField(default=0)
    area_precinct_match_count = models.IntegerField(default=0)
    warning_count = models.IntegerField(default=0)

    def __str__(self):
        return f'Crime import run {self.id} - {self.status}'


class CrimeArea(models.Model):
    data_source = models.ForeignKey(DataSource, on_delete=models.PROTECT)
    import_run = models.ForeignKey(CrimeImportRun, on_delete=models.PROTECT)

    area_name = models.CharField(max_length=255, db_index=True)
    normalized_name = models.CharField(max_length=255, db_index=True)
    area_type = models.CharField(max_length=100, default='suburb')

    municipality = models.CharField(max_length=255, blank=True, default='City of Cape Town')
    province = models.CharField(max_length=255, blank=True, default='Western Cape')

    source_object_id = models.CharField(max_length=100, blank=True, default='')
    source_object_id_1 = models.CharField(max_length=100, blank=True, default='')

    shape_area = models.FloatField(null=True, blank=True)
    shape_length = models.FloatField(null=True, blank=True)
    shape_length_alt = models.FloatField(null=True, blank=True)

    geometry = models.JSONField()
    raw_properties = models.JSONField(default=dict)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['normalized_name']),
            models.Index(fields=['province', 'municipality']),
        ]

    def __str__(self):
        return self.area_name


class SapsPrecinct(models.Model):
    data_source = models.ForeignKey(DataSource, on_delete=models.PROTECT)
    import_run = models.ForeignKey(CrimeImportRun, on_delete=models.PROTECT)

    precinct_name = models.CharField(max_length=255, db_index=True)
    normalized_name = models.CharField(max_length=255, db_index=True)
    component_name = models.CharField(max_length=255, blank=True, default='')

    province = models.CharField(max_length=255, blank=True, default='Western Cape')

    source_object_id = models.CharField(max_length=100, blank=True, default='')
    source_create_date = models.CharField(max_length=100, blank=True, default='')

    shape_area = models.FloatField(null=True, blank=True)
    shape_length = models.FloatField(null=True, blank=True)

    geometry = models.JSONField()
    raw_properties = models.JSONField(default=dict)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['normalized_name']),
            models.Index(fields=['province']),
        ]

    def __str__(self):
        return self.precinct_name


class AreaPrecinctMatch(models.Model):
    import_run = models.ForeignKey(CrimeImportRun, on_delete=models.PROTECT)

    crime_area = models.ForeignKey(CrimeArea, on_delete=models.CASCADE, related_name='precinct_matches')
    saps_precinct = models.ForeignKey(SapsPrecinct, on_delete=models.CASCADE, related_name='area_matches')

    overlap_area = models.FloatField(null=True, blank=True)
    overlap_percent = models.FloatField()
    is_primary = models.BooleanField(default=False)

    match_method = models.CharField(max_length=100, default='geometry_overlap')
    notes = models.TextField(blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['crime_area', 'is_primary']),
            models.Index(fields=['saps_precinct']),
        ]

    def __str__(self):
        return f'{self.crime_area.area_name} -> {self.saps_precinct.precinct_name} ({self.overlap_percent}%)'


class SapsCrimeMetric(models.Model):
    data_source = models.ForeignKey(DataSource, on_delete=models.PROTECT)
    import_run = models.ForeignKey(CrimeImportRun, on_delete=models.PROTECT)

    saps_precinct = models.ForeignKey(SapsPrecinct, null=True, blank=True, on_delete=models.SET_NULL, related_name='crime_metrics')

    comp_level = models.CharField(max_length=100, blank=True, default='')
    station_name = models.CharField(max_length=255, db_index=True)
    normalized_station_name = models.CharField(max_length=255, db_index=True)
    station_crime_category = models.CharField(max_length=500, blank=True, default='')

    district = models.CharField(max_length=255, blank=True, default='')
    province = models.CharField(max_length=255, blank=True, default='')

    crime_category = models.CharField(max_length=255, db_index=True)
    crime_code = models.CharField(max_length=100, blank=True, default='')
    crime_group = models.CharField(max_length=255, blank=True, default='')
    count_direction = models.CharField(max_length=100, blank=True, default='')

    period_name = models.CharField(max_length=255, db_index=True)
    period_type = models.CharField(max_length=100, default='quarter_total')
    period_start = models.DateField(null=True, blank=True)
    period_end = models.DateField(null=True, blank=True)

    incidents = models.IntegerField()

    crime_category_national_placement = models.CharField(max_length=100, blank=True, default='')
    crime_category_provincial_placement = models.CharField(max_length=100, blank=True, default='')
    national_contribution_placement = models.CharField(max_length=100, blank=True, default='')
    national_count_diff_placement = models.CharField(max_length=100, blank=True, default='')
    provincial_contribution_placement = models.CharField(max_length=100, blank=True, default='')
    provincial_count_diff_placement = models.CharField(max_length=100, blank=True, default='')

    source_sheet = models.CharField(max_length=255, default='RAW Data')
    source_row = models.IntegerField(null=True, blank=True)
    source_column = models.CharField(max_length=255, blank=True, default='')
    source_no = models.CharField(max_length=100, blank=True, default='')

    raw_values = models.JSONField(default=dict)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['station_name']),
            models.Index(fields=['normalized_station_name']),
            models.Index(fields=['crime_category']),
            models.Index(fields=['crime_group']),
            models.Index(fields=['period_name']),
            models.Index(fields=['period_type']),
            models.Index(fields=['province', 'district']),
            models.Index(fields=['period_start', 'period_end']),
        ]

    def __str__(self):
        return f'{self.station_name} - {self.crime_category} - {self.period_name}: {self.incidents}'


class PlaceAlias(models.Model):
    crime_area = models.ForeignKey(CrimeArea, on_delete=models.CASCADE, related_name='aliases')

    alias_name = models.CharField(max_length=255, db_index=True)
    normalized_alias = models.CharField(max_length=255, db_index=True)

    match_type = models.CharField(max_length=100, default='exact')
    confidence = models.CharField(max_length=50, default='high')
    reviewed = models.BooleanField(default=False)

    notes = models.TextField(blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['normalized_alias']),
        ]

    def __str__(self):
        return f'{self.alias_name} -> {self.crime_area.area_name}'


class ImportWarning(models.Model):
    import_run = models.ForeignKey(CrimeImportRun, on_delete=models.CASCADE, related_name='warnings')

    warning_type = models.CharField(max_length=100)
    message = models.TextField()

    source_file_name = models.CharField(max_length=255, blank=True, default='')
    source_row = models.IntegerField(null=True, blank=True)
    raw_values = models.JSONField(default=dict)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.warning_type
