public with sharing class SampleAction {
    @InvocableMethod(label='Run synthetic example')
    public static void run(List<Account> records) {
        List<Contact> contacts = [SELECT Id, LastName FROM Contact WHERE AccountId != null];
        update records;
    }
}
